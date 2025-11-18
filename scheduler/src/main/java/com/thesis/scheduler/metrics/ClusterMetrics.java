package com.thesis.scheduler.metrics;

import io.kubernetes.client.openapi.ApiException;
import io.kubernetes.client.openapi.apis.CoreV1Api;
import io.kubernetes.client.openapi.models.*;

import java.util.Random;

/**
 * Collects and provides cluster metrics
 * 
 * Currently uses simulated metrics for CPU.
 * In production, integrate with Kubernetes Metrics Server.
 */
public class ClusterMetrics {
    
    private final CoreV1Api api;
    private final Random random = new Random();
    private long requestCounter = 0;
    
    public ClusterMetrics(CoreV1Api api) {
        this.api = api;
    }
    
    /**
     * Get CPU usage for a specific node
     * 
     * NOTE: This is simulated. For real metrics, integrate with:
     * kubectl top nodes
     * or Metrics Server API
     */
    public double getNodeCpuUsage(V1Node node) {
        try {
            String nodeName = node.getMetadata().getName();
            
            // Count pods on this node
            V1PodList pods = api.listPodForAllNamespaces(
                null, null, "spec.nodeName=" + nodeName, null,
                null, null, null, null, null, null, null
            );
            
            int podCount = pods.getItems().size();
            
            // Simulate: more pods = higher CPU
            double baseCpu = 20.0 + (podCount * 12.0);
            double noise = random.nextDouble() * 10.0;
            
            return Math.min(95.0, baseCpu + noise);
            
        } catch (ApiException e) {
            // Default value on error
            return 50.0;
        }
    }
    
    /**
     * Get average CPU usage across all nodes
     */
    public double getAverageClusterCpuUsage() {
        try {
            V1NodeList nodeList = api.listNode(
                null, null, null, null, null, null,
                null, null, null, null, null
            );
            
            double totalCpu = 0.0;
            int count = 0;
            
            for (V1Node node : nodeList.getItems()) {
                totalCpu += getNodeCpuUsage(node);
                count++;
            }
            
            if (count == 0) {
                return 50.0;
            }
            
            // Add dynamic variation based on time
            requestCounter++;
            double variation = getSimulatedVariation();
            
            return Math.min(95.0, (totalCpu / count) + variation);
            
        } catch (ApiException e) {
            return 50.0;
        }
    }
    
    /**
     * Simulate CPU variation over time for adaptive behavior
     * This creates a realistic pattern:
     * - Starts low
     * - Gradually increases
     * - Peaks high
     * - Decreases back
     */
    private double getSimulatedVariation() {
        long cycle = requestCounter % 100;
        
        if (cycle < 25) {
            // Low load period
            return -10.0;
        } else if (cycle < 50) {
            // Increasing load
            return ((cycle - 25) * 1.2);
        } else if (cycle < 75) {
            // High load period
            return 20.0;
        } else {
            // Decreasing load
            return 20.0 - ((cycle - 75) * 0.8);
        }
    }
    
    /**
     * Get memory usage for a node (placeholder)
     */
    public double getNodeMemoryUsage(V1Node node) {
        V1NodeStatus status = node.getStatus();
        if (status == null || status.getAllocatable() == null) {
            return 0.0;
        }
        
        // This would require Metrics Server in production
        return 50.0; // Placeholder
    }
}