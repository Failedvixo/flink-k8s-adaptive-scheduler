package com.thesis.scheduler.metrics;

import io.kubernetes.client.openapi.ApiClient;
import io.kubernetes.client.openapi.ApiException;
import io.kubernetes.client.openapi.Configuration;
import io.kubernetes.client.openapi.apis.CoreV1Api;
import io.kubernetes.client.openapi.apis.CustomObjectsApi;
import io.kubernetes.client.openapi.models.*;
import io.kubernetes.client.Metrics;
import io.kubernetes.client.custom.NodeMetrics;
import io.kubernetes.client.custom.NodeMetricsList;
import io.kubernetes.client.custom.PodMetrics;
import io.kubernetes.client.custom.PodMetricsList;
import io.kubernetes.client.custom.ContainerMetrics;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.math.BigDecimal;
import java.util.HashMap;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;

/**
 * Collects REAL cluster metrics from Kubernetes Metrics Server.
 * 
 * This class uses the Kubernetes Metrics API (metrics.k8s.io/v1beta1)
 * to obtain actual CPU and memory usage from nodes and pods.
 * 
 * Prerequisites:
 * - Metrics Server must be installed in the cluster
 * - For Minikube: minikube addons enable metrics-server
 * 
 * @author Vicente (Thesis Project)
 */
public class ClusterMetrics {
    
    private static final Logger LOG = LoggerFactory.getLogger(ClusterMetrics.class);
    
    private final CoreV1Api coreApi;
    private final Metrics metricsApi;
    
    // Cache for metrics (to avoid excessive API calls)
    private final Map<String, CachedMetric> nodeMetricsCache = new ConcurrentHashMap<>();
    private final Map<String, CachedMetric> podMetricsCache = new ConcurrentHashMap<>();
    
    // Cache TTL in milliseconds (5 seconds)
    private static final long CACHE_TTL_MS = 5000;
    
    // Flag to track if metrics server is available
    private boolean metricsServerAvailable = true;
    
    public ClusterMetrics(CoreV1Api coreApi) {
        this.coreApi = coreApi;
        this.metricsApi = new Metrics(Configuration.getDefaultApiClient());
        
        // Test metrics server availability
        testMetricsServerAvailability();
    }
    
    /**
     * Test if the Metrics Server is available.
     */
    private void testMetricsServerAvailability() {
        try {
            metricsApi.getNodeMetrics();
            metricsServerAvailable = true;
            LOG.info("[ClusterMetrics] ✓ Metrics Server is available - using REAL metrics");
        } catch (ApiException e) {
            metricsServerAvailable = false;
            LOG.warn("[ClusterMetrics] ✗ Metrics Server not available (code: {})", e.getCode());
            LOG.warn("[ClusterMetrics] Install with: minikube addons enable metrics-server");
            LOG.warn("[ClusterMetrics] Falling back to estimated metrics based on pod count");
        }
    }
    
    /**
     * Get CPU usage for a specific node (percentage 0-100).
     * 
     * @param node The Kubernetes node
     * @return CPU usage percentage
     */
    public double getNodeCpuUsage(V1Node node) {
        String nodeName = node.getMetadata().getName();
        return getNodeCpuUsage(nodeName);
    }
    
    /**
     * Get CPU usage for a specific node by name (percentage 0-100).
     * 
     * @param nodeName Name of the node
     * @return CPU usage percentage
     */
    public double getNodeCpuUsage(String nodeName) {
        // Check cache first
        CachedMetric cached = nodeMetricsCache.get(nodeName);
        if (cached != null && !cached.isExpired()) {
            return cached.cpuPercent;
        }
        
        if (metricsServerAvailable) {
            try {
                return getRealNodeCpuUsage(nodeName);
            } catch (ApiException e) {
                LOG.debug("[ClusterMetrics] Failed to get real metrics for {}: {}", nodeName, e.getMessage());
                return getEstimatedNodeCpuUsage(nodeName);
            }
        } else {
            return getEstimatedNodeCpuUsage(nodeName);
        }
    }
    
    /**
     * Get REAL CPU usage from Metrics Server.
     */
    private double getRealNodeCpuUsage(String nodeName) throws ApiException {
        NodeMetricsList metricsList = metricsApi.getNodeMetrics();
        
        for (NodeMetrics nodeMetrics : metricsList.getItems()) {
            if (nodeName.equals(nodeMetrics.getMetadata().getName())) {
                // Get CPU usage in nanocores
                BigDecimal cpuUsage = nodeMetrics.getUsage().get("cpu").getNumber();
                
                // Get allocatable CPU from node spec
                V1Node node = coreApi.readNode(nodeName, null);
                BigDecimal allocatableCpu = node.getStatus().getAllocatable().get("cpu").getNumber();
                
                // Convert to percentage
                // CPU is in nanocores (n) or millicores (m)
                double usageValue = parseCpuValue(cpuUsage.toString());
                double allocatableValue = parseCpuValue(allocatableCpu.toString());
                
                double cpuPercent = (usageValue / allocatableValue) * 100.0;
                cpuPercent = Math.min(100.0, Math.max(0.0, cpuPercent));
                
                // Cache the result
                nodeMetricsCache.put(nodeName, new CachedMetric(cpuPercent, 0));
                
                LOG.debug("[ClusterMetrics] Node {} CPU: {:.1f}% (real)", nodeName, cpuPercent);
                return cpuPercent;
            }
        }
        
        LOG.warn("[ClusterMetrics] No metrics found for node {}", nodeName);
        return 50.0; // Default
    }
    
    /**
     * Estimate CPU usage based on pod count (fallback when Metrics Server unavailable).
     */
    private double getEstimatedNodeCpuUsage(String nodeName) {
        try {
            // Count pods on this node
            V1PodList pods = coreApi.listPodForAllNamespaces(
                null, null, "spec.nodeName=" + nodeName, null,
                null, null, null, null, null, null, null
            );
            
            int podCount = pods.getItems().size();
            
            // Estimate: base 15% + 8% per pod, capped at 90%
            double estimated = 15.0 + (podCount * 8.0);
            estimated = Math.min(90.0, estimated);
            
            LOG.debug("[ClusterMetrics] Node {} CPU: {:.1f}% (estimated from {} pods)", 
                nodeName, estimated, podCount);
            
            return estimated;
            
        } catch (ApiException e) {
            LOG.error("[ClusterMetrics] Error estimating CPU for {}: {}", nodeName, e.getMessage());
            return 50.0;
        }
    }
    
    /**
     * Get CPU usage for all nodes.
     * 
     * @return Map of node name to CPU percentage
     */
    public Map<String, Double> getNodeCpuUsage() {
        Map<String, Double> result = new HashMap<>();
        
        if (metricsServerAvailable) {
            try {
                NodeMetricsList metricsList = metricsApi.getNodeMetrics();
                V1NodeList nodeList = coreApi.listNode(null, null, null, null, null, 
                    null, null, null, null, null, null);
                
                // Build map of allocatable CPU per node
                Map<String, Double> allocatableMap = new HashMap<>();
                for (V1Node node : nodeList.getItems()) {
                    String name = node.getMetadata().getName();
                    BigDecimal alloc = node.getStatus().getAllocatable().get("cpu").getNumber();
                    allocatableMap.put(name, parseCpuValue(alloc.toString()));
                }
                
                // Calculate percentage for each node
                for (NodeMetrics metrics : metricsList.getItems()) {
                    String nodeName = metrics.getMetadata().getName();
                    BigDecimal usage = metrics.getUsage().get("cpu").getNumber();
                    
                    double usageValue = parseCpuValue(usage.toString());
                    double allocatable = allocatableMap.getOrDefault(nodeName, 1000.0);
                    
                    double cpuPercent = (usageValue / allocatable) * 100.0;
                    cpuPercent = Math.min(100.0, Math.max(0.0, cpuPercent));
                    
                    result.put(nodeName, cpuPercent);
                    nodeMetricsCache.put(nodeName, new CachedMetric(cpuPercent, 0));
                }
                
                LOG.debug("[ClusterMetrics] Retrieved real CPU metrics for {} nodes", result.size());
                return result;
                
            } catch (ApiException e) {
                LOG.warn("[ClusterMetrics] Failed to get real metrics: {}", e.getMessage());
                metricsServerAvailable = false;
            }
        }
        
        // Fallback to estimation
        return getEstimatedNodeCpuUsage();
    }
    
    /**
     * Fallback: estimate CPU for all nodes.
     */
    private Map<String, Double> getEstimatedNodeCpuUsage() {
        Map<String, Double> result = new HashMap<>();
        
        try {
            V1NodeList nodeList = coreApi.listNode(null, null, null, null, null, 
                null, null, null, null, null, null);
            
            for (V1Node node : nodeList.getItems()) {
                String nodeName = node.getMetadata().getName();
                result.put(nodeName, getEstimatedNodeCpuUsage(nodeName));
            }
            
        } catch (ApiException e) {
            LOG.error("[ClusterMetrics] Error getting node list: {}", e.getMessage());
        }
        
        return result;
    }
    
    /**
     * Get average CPU usage across all nodes.
     * 
     * @return Average CPU percentage
     */
    public double getAverageClusterCpuUsage() {
        Map<String, Double> nodeUsage = getNodeCpuUsage();
        
        if (nodeUsage.isEmpty()) {
            return 50.0;
        }
        
        double total = nodeUsage.values().stream().mapToDouble(Double::doubleValue).sum();
        double average = total / nodeUsage.size();
        
        LOG.debug("[ClusterMetrics] Cluster average CPU: {:.1f}%", average);
        return average;
    }
    
    /**
     * Get CPU usage for a specific pod.
     * 
     * @param namespace Pod namespace
     * @param podName Pod name
     * @return CPU usage in millicores
     */
    public double getPodCpuUsage(String namespace, String podName) {
        String cacheKey = namespace + "/" + podName;
        
        CachedMetric cached = podMetricsCache.get(cacheKey);
        if (cached != null && !cached.isExpired()) {
            return cached.cpuPercent;
        }
        
        if (!metricsServerAvailable) {
            return 100.0; // Default 100 millicores
        }
        
        try {
            PodMetricsList metricsList = metricsApi.getPodMetrics(namespace);
            
            for (PodMetrics podMetrics : metricsList.getItems()) {
                if (podName.equals(podMetrics.getMetadata().getName())) {
                    double totalCpu = 0.0;
                    
                    for (ContainerMetrics container : podMetrics.getContainers()) {
                        BigDecimal cpu = container.getUsage().get("cpu").getNumber();
                        totalCpu += parseCpuValue(cpu.toString());
                    }
                    
                    podMetricsCache.put(cacheKey, new CachedMetric(totalCpu, 0));
                    return totalCpu;
                }
            }
            
        } catch (ApiException e) {
            LOG.debug("[ClusterMetrics] Failed to get pod metrics: {}", e.getMessage());
        }
        
        return 100.0; // Default
    }
    
    /**
     * Get memory usage for a node (percentage 0-100).
     * 
     * @param node The Kubernetes node
     * @return Memory usage percentage
     */
    public double getNodeMemoryUsage(V1Node node) {
        String nodeName = node.getMetadata().getName();
        
        if (!metricsServerAvailable) {
            return 50.0;
        }
        
        try {
            NodeMetricsList metricsList = metricsApi.getNodeMetrics();
            
            for (NodeMetrics nodeMetrics : metricsList.getItems()) {
                if (nodeName.equals(nodeMetrics.getMetadata().getName())) {
                    BigDecimal memUsage = nodeMetrics.getUsage().get("memory").getNumber();
                    BigDecimal memAllocatable = node.getStatus().getAllocatable().get("memory").getNumber();
                    
                    double usageBytes = parseMemoryValue(memUsage.toString());
                    double allocatableBytes = parseMemoryValue(memAllocatable.toString());
                    
                    double memPercent = (usageBytes / allocatableBytes) * 100.0;
                    return Math.min(100.0, Math.max(0.0, memPercent));
                }
            }
            
        } catch (ApiException e) {
            LOG.debug("[ClusterMetrics] Failed to get memory metrics: {}", e.getMessage());
        }
        
        return 50.0;
    }
    
    /**
     * Parse CPU value from Kubernetes format.
     * Formats: "100m" (millicores), "1" (cores), "1000000000n" (nanocores)
     * 
     * @param cpuString CPU value string
     * @return CPU value in millicores
     */
    private double parseCpuValue(String cpuString) {
        if (cpuString == null || cpuString.isEmpty()) {
            return 0.0;
        }
        
        cpuString = cpuString.trim();
        
        try {
            if (cpuString.endsWith("n")) {
                // Nanocores to millicores
                return Double.parseDouble(cpuString.replace("n", "")) / 1_000_000.0;
            } else if (cpuString.endsWith("m")) {
                // Already in millicores
                return Double.parseDouble(cpuString.replace("m", ""));
            } else {
                // Cores to millicores
                return Double.parseDouble(cpuString) * 1000.0;
            }
        } catch (NumberFormatException e) {
            LOG.warn("[ClusterMetrics] Failed to parse CPU value: {}", cpuString);
            return 0.0;
        }
    }
    
    /**
     * Parse memory value from Kubernetes format.
     * Formats: "1Gi", "1024Mi", "1048576Ki", "1073741824"
     * 
     * @param memString Memory value string
     * @return Memory value in bytes
     */
    private double parseMemoryValue(String memString) {
        if (memString == null || memString.isEmpty()) {
            return 0.0;
        }
        
        memString = memString.trim();
        
        try {
            if (memString.endsWith("Ki")) {
                return Double.parseDouble(memString.replace("Ki", "")) * 1024.0;
            } else if (memString.endsWith("Mi")) {
                return Double.parseDouble(memString.replace("Mi", "")) * 1024.0 * 1024.0;
            } else if (memString.endsWith("Gi")) {
                return Double.parseDouble(memString.replace("Gi", "")) * 1024.0 * 1024.0 * 1024.0;
            } else if (memString.endsWith("Ti")) {
                return Double.parseDouble(memString.replace("Ti", "")) * 1024.0 * 1024.0 * 1024.0 * 1024.0;
            } else {
                return Double.parseDouble(memString);
            }
        } catch (NumberFormatException e) {
            LOG.warn("[ClusterMetrics] Failed to parse memory value: {}", memString);
            return 0.0;
        }
    }
    
    /**
     * Check if Metrics Server is available.
     */
    public boolean isMetricsServerAvailable() {
        return metricsServerAvailable;
    }
    
    /**
     * Force refresh of metrics availability check.
     */
    public void refreshMetricsAvailability() {
        testMetricsServerAvailability();
    }
    
    /**
     * Get a summary of current cluster metrics.
     */
    public String getMetricsSummary() {
        StringBuilder sb = new StringBuilder();
        sb.append("\n========================================\n");
        sb.append("  CLUSTER METRICS SUMMARY\n");
        sb.append("========================================\n");
        sb.append(String.format("Metrics Server: %s\n", 
            metricsServerAvailable ? "✓ Available (real metrics)" : "✗ Unavailable (estimated)"));
        sb.append("\nNode CPU Usage:\n");
        
        Map<String, Double> cpuUsage = getNodeCpuUsage();
        for (Map.Entry<String, Double> entry : cpuUsage.entrySet()) {
            sb.append(String.format("  %s: %.1f%%\n", entry.getKey(), entry.getValue()));
        }
        
        sb.append(String.format("\nCluster Average: %.1f%%\n", getAverageClusterCpuUsage()));
        sb.append("========================================\n");
        
        return sb.toString();
    }
    
    /**
     * Inner class for cached metrics with TTL.
     */
    private static class CachedMetric {
        final double cpuPercent;
        final double memoryPercent;
        final long timestamp;
        
        CachedMetric(double cpuPercent, double memoryPercent) {
            this.cpuPercent = cpuPercent;
            this.memoryPercent = memoryPercent;
            this.timestamp = System.currentTimeMillis();
        }
        
        boolean isExpired() {
            return System.currentTimeMillis() - timestamp > CACHE_TTL_MS;
        }
    }
}