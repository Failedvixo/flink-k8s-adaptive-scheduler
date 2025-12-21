package com.thesis.scheduler;

import io.kubernetes.client.openapi.ApiClient;
import io.kubernetes.client.openapi.ApiException;
import io.kubernetes.client.openapi.Configuration;
import io.kubernetes.client.openapi.apis.CoreV1Api;
import io.kubernetes.client.openapi.models.*;
import io.kubernetes.client.util.Config;
import com.thesis.scheduler.strategy.*;
import com.thesis.scheduler.metrics.*;
import com.thesis.scheduler.model.*;

import java.io.IOException;
import java.util.*;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.stream.Collectors;

/**
 * Adaptive Scheduler for Flink TaskManagers in Kubernetes
 * 
 * This is the MAIN contribution of the thesis:
 * - Monitors cluster CPU usage (REAL metrics from Metrics Server)
 * - Dynamically switches scheduling strategies (including BANDIT)
 * - Assigns Flink TaskManager pods to optimal nodes
 * - Records all decisions for analysis
 * 
 * Now supports Multi-Armed Bandit (UCB1) strategy!
 * 
 * @author Vicente (Thesis Project)
 */
public class AdaptiveScheduler {
    
    private static final String SCHEDULER_NAME = "adaptive-scheduler";
    private static final String FLINK_COMPONENT_LABEL = "component=taskmanager";
    
    private final CoreV1Api api;
    private final ClusterMetrics clusterMetrics;
    private final Map<SchedulingStrategyType, SchedulingStrategy> strategies;
    private final List<SchedulingDecision> decisionHistory;
    
    private volatile SchedulingStrategyType currentStrategy;
    private final AtomicInteger schedulingCount;
    private long lastStrategySwitchTime;
    
    // Configuration from environment
    private final double cpuLowThreshold;
    private final double cpuHighThreshold;
    private final long strategyCooldownMs;
    
    // Fixed strategy mode (for experiments)
    private final boolean useFixedStrategy;
    private final SchedulingStrategyType fixedStrategy;
    
    public AdaptiveScheduler() throws IOException {
        // Initialize Kubernetes client
        ApiClient client = Config.defaultClient();
        Configuration.setDefaultApiClient(client);
        this.api = new CoreV1Api();
        
        // Initialize components
        this.clusterMetrics = new ClusterMetrics(api);
        this.strategies = initializeStrategies();
        this.decisionHistory = Collections.synchronizedList(new ArrayList<>());
        this.schedulingCount = new AtomicInteger(0);
        
        // Load configuration from environment
        this.cpuLowThreshold = Double.parseDouble(
            System.getenv().getOrDefault("CPU_LOW_THRESHOLD", "40.0"));
        this.cpuHighThreshold = Double.parseDouble(
            System.getenv().getOrDefault("CPU_HIGH_THRESHOLD", "80.0"));
        this.strategyCooldownMs = Long.parseLong(
            System.getenv().getOrDefault("STRATEGY_COOLDOWN", "30")) * 1000;
        
        // Check for fixed strategy mode (for experiments)
        String fixedStrategyEnv = System.getenv("FIXED_STRATEGY");
        if (fixedStrategyEnv != null && !fixedStrategyEnv.isEmpty()) {
            this.useFixedStrategy = true;
            this.fixedStrategy = SchedulingStrategyType.valueOf(fixedStrategyEnv.toUpperCase());
            this.currentStrategy = this.fixedStrategy;
        } else {
            this.useFixedStrategy = false;
            this.fixedStrategy = null;
            this.currentStrategy = SchedulingStrategyType.FCFS;
        }
        
        this.lastStrategySwitchTime = System.currentTimeMillis();
        
        printBanner();
    }
    
    /**
     * Main scheduler loop
     */
    public void run() throws Exception {
        System.out.println("Starting scheduler loop...\n");
        
        // Print initial cluster metrics
        printClusterMetrics();
        
        while (true) {
            try {
                // 1. Find unscheduled Flink TaskManager pods
                List<V1Pod> pendingPods = getUnscheduledTaskManagers();
                
                if (!pendingPods.isEmpty()) {
                    System.out.println("\n[" + getCurrentTimestamp() + "] Found " + 
                        pendingPods.size() + " pending TaskManager(s)");
                    
                    for (V1Pod pod : pendingPods) {
                        schedulePod(pod);
                    }
                    
                    // Print bandit statistics after scheduling if using BANDIT
                    if (currentStrategy == SchedulingStrategyType.BANDIT) {
                        BanditStrategy bandit = (BanditStrategy) strategies.get(SchedulingStrategyType.BANDIT);
                        System.out.println(bandit.getStatisticsSummary());
                    }
                }
                
                // 2. Check if we should switch strategy (only if not in fixed mode)
                if (!useFixedStrategy) {
                    checkAndSwitchStrategy();
                }
                
                // 3. Sleep before next iteration
                Thread.sleep(2000);
                
            } catch (Exception e) {
                System.err.println("[ERROR] " + e.getMessage());
                e.printStackTrace();
                Thread.sleep(5000);
            }
        }
    }
    
    /**
     * Print cluster metrics status
     */
    private void printClusterMetrics() {
        System.out.println("========================================");
        System.out.println("  CLUSTER METRICS STATUS");
        System.out.println("========================================");
        
        if (clusterMetrics.isMetricsServerAvailable()) {
            System.out.println("  Metrics Server: ✓ AVAILABLE (using REAL metrics)");
        } else {
            System.out.println("  Metrics Server: ✗ NOT AVAILABLE (using estimated metrics)");
            System.out.println("  To enable: minikube addons enable metrics-server");
        }
        
        System.out.println();
        System.out.println("  Current Node CPU Usage:");
        
        Map<String, Double> nodeMetrics = clusterMetrics.getNodeCpuUsage();
        for (Map.Entry<String, Double> entry : nodeMetrics.entrySet()) {
            System.out.println("    " + entry.getKey() + ": " + 
                String.format("%.1f%%", entry.getValue()));
        }
        
        System.out.println();
        System.out.println("  Cluster Average: " + 
            String.format("%.1f%%", clusterMetrics.getAverageClusterCpuUsage()));
        System.out.println("========================================\n");
    }
    
    /**
     * CORE METHOD: Schedule a pod to a node
     * This is where the actual resource scheduling happens
     */
    private void schedulePod(V1Pod pod) throws ApiException {
        String podName = pod.getMetadata().getName();
        String namespace = pod.getMetadata().getNamespace();
        
        System.out.println("\n[SCHEDULING] Pod: " + podName);
        System.out.println("  Strategy: " + currentStrategy);
        
        // Get available nodes
        List<V1Node> nodes = getAvailableNodes();
        
        if (nodes.isEmpty()) {
            System.out.println("  Result: NO NODES AVAILABLE");
            return;
        }
        
        // Select node using current strategy
        SchedulingStrategy strategy = strategies.get(currentStrategy);
        V1Node selectedNode = strategy.selectNode(nodes, pod, clusterMetrics);
        
        if (selectedNode == null) {
            System.out.println("  Result: NO SUITABLE NODE FOUND");
            return;
        }
        
        String nodeName = selectedNode.getMetadata().getName();
        double nodeCpu = clusterMetrics.getNodeCpuUsage(nodeName);
        
        // Bind pod to node (ACTUAL RESOURCE SCHEDULING)
        V1Binding binding = new V1Binding()
            .metadata(new V1ObjectMeta().name(podName))
            .target(new V1ObjectReference()
                .kind("Node")
                .apiVersion("v1")
                .name(nodeName));
        
        try {
            api.createNamespacedBinding(namespace, binding, null, null, null, null);
            
            int count = schedulingCount.incrementAndGet();
            
            System.out.println("  Result: ✓ SCHEDULED to " + nodeName);
            System.out.println("  Node CPU: " + String.format("%.1f%%", nodeCpu) + 
                (clusterMetrics.isMetricsServerAvailable() ? " (real)" : " (estimated)"));
            System.out.println("  Total scheduled: " + count);
            
            // Record decision for analysis
            recordDecision(pod, selectedNode, currentStrategy, nodeCpu);
            
        } catch (ApiException e) {
            System.err.println("  Error: Failed to bind pod - Code: " + e.getCode() + 
                " Body: " + e.getResponseBody());
        }
    }
    
    /**
     * Check cluster metrics and switch strategy if needed
     */
    private void checkAndSwitchStrategy() {
        // Enforce cooldown period
        long now = System.currentTimeMillis();
        if (now - lastStrategySwitchTime < strategyCooldownMs) {
            return;
        }
        
        double avgCpu = clusterMetrics.getAverageClusterCpuUsage();
        SchedulingStrategyType newStrategy = selectStrategyForCpu(avgCpu);
        
        if (newStrategy != currentStrategy) {
            System.out.println("\n[STRATEGY SWITCH]");
            System.out.println("  From: " + currentStrategy);
            System.out.println("  To: " + newStrategy);
            System.out.println("  Reason: Cluster CPU = " + String.format("%.1f%%", avgCpu) +
                (clusterMetrics.isMetricsServerAvailable() ? " (real)" : " (estimated)"));
            System.out.println("  Time: " + getCurrentTimestamp());
            
            currentStrategy = newStrategy;
            lastStrategySwitchTime = now;
        }
    }
    
    /**
     * Select optimal strategy based on CPU usage
     */
    private SchedulingStrategyType selectStrategyForCpu(double cpuUsage) {
        if (cpuUsage > 60.0) {
            return SchedulingStrategyType.BANDIT;      // Alta carga: usar Bandit
        } else if (cpuUsage > 30.0) {
            return SchedulingStrategyType.LEAST_LOADED; // Media carga: nodo menos cargado
        } else {
            return SchedulingStrategyType.FCFS;         // Baja carga: simple y rápido
    }
}
    
    /**
     * Get unscheduled Flink TaskManager pods
     */
    private List<V1Pod> getUnscheduledTaskManagers() throws ApiException {
        V1PodList podList = api.listPodForAllNamespaces(
            null, null, null, FLINK_COMPONENT_LABEL, 
            null, null, null, null, null, null, null
        );
        
        return podList.getItems().stream()
            .filter(pod -> {
                String schedulerName = pod.getSpec().getSchedulerName();
                String nodeName = pod.getSpec().getNodeName();
                return SCHEDULER_NAME.equals(schedulerName) && 
                       (nodeName == null || nodeName.isEmpty());
            })
            .collect(Collectors.toList());
    }
    
    /**
     * Get available nodes for scheduling
     */
    private List<V1Node> getAvailableNodes() throws ApiException {
        V1NodeList nodeList = api.listNode(
            null, null, null, null, null, null, null, null, null, null, null
        );
        
        return nodeList.getItems().stream()
            .filter(this::isNodeReady)
            .filter(node -> !isNodeTainted(node))
            .collect(Collectors.toList());
    }
    
    /**
     * Check if node is ready
     */
    private boolean isNodeReady(V1Node node) {
        V1NodeStatus status = node.getStatus();
        if (status == null || status.getConditions() == null) {
            return false;
        }
        
        return status.getConditions().stream()
            .anyMatch(c -> "Ready".equals(c.getType()) && "True".equals(c.getStatus()));
    }
    
    /**
     * Check if node has taints preventing scheduling
     */
    private boolean isNodeTainted(V1Node node) {
        V1NodeSpec spec = node.getSpec();
        if (spec == null || spec.getTaints() == null) {
            return false;
        }
        
        return spec.getTaints().stream()
            .anyMatch(t -> "NoSchedule".equals(t.getEffect()) || 
                          "NoExecute".equals(t.getEffect()));
    }
    
    /**
     * Initialize all scheduling strategies
     */
    private Map<SchedulingStrategyType, SchedulingStrategy> initializeStrategies() {
        Map<SchedulingStrategyType, SchedulingStrategy> map = new HashMap<>();
        map.put(SchedulingStrategyType.FCFS, new FCFSStrategy());
        map.put(SchedulingStrategyType.LEAST_LOADED, new LeastLoadedStrategy());
        map.put(SchedulingStrategyType.PRIORITY, new PriorityStrategy());
        map.put(SchedulingStrategyType.BALANCED, new BalancedStrategy());
        map.put(SchedulingStrategyType.BANDIT, new BanditStrategy());  // NEW: Bandit strategy
        return map;
    }
    
    /**
     * Record scheduling decision
     */
    private void recordDecision(V1Pod pod, V1Node node, 
                                SchedulingStrategyType strategy, double nodeCpu) {
        SchedulingDecision decision = new SchedulingDecision(
            pod.getMetadata().getName(),
            node.getMetadata().getName(),
            strategy,
            nodeCpu,
            System.currentTimeMillis()
        );
        decisionHistory.add(decision);
    }
    
    /**
     * Print statistics on shutdown
     */
    public void printStatistics() {
        System.out.println("\n========================================");
        System.out.println("     SCHEDULING STATISTICS");
        System.out.println("========================================");
        System.out.println("Total Pods Scheduled: " + schedulingCount.get());
        System.out.println("Current Strategy: " + currentStrategy);
        System.out.println("Fixed Strategy Mode: " + (useFixedStrategy ? "YES (" + fixedStrategy + ")" : "NO (Adaptive)"));
        System.out.println("Metrics Source: " + 
            (clusterMetrics.isMetricsServerAvailable() ? "REAL (Metrics Server)" : "ESTIMATED"));
        System.out.println();
        
        // Count by strategy
        Map<SchedulingStrategyType, Long> countByStrategy = decisionHistory.stream()
            .collect(Collectors.groupingBy(
                SchedulingDecision::getStrategy, 
                Collectors.counting()
            ));
        
        System.out.println("Distribution by Strategy:");
        for (Map.Entry<SchedulingStrategyType, Long> entry : countByStrategy.entrySet()) {
            double percentage = (entry.getValue() * 100.0) / decisionHistory.size();
            System.out.println("  " + entry.getKey() + ": " + entry.getValue() + 
                             " (" + String.format("%.1f%%", percentage) + ")");
        }
        
        // If using Bandit, print bandit-specific statistics
        if (countByStrategy.containsKey(SchedulingStrategyType.BANDIT)) {
            BanditStrategy bandit = (BanditStrategy) strategies.get(SchedulingStrategyType.BANDIT);
            System.out.println(bandit.getStatisticsSummary());
        }
        
        System.out.println("========================================\n");
    }
    
    /**
     * Print banner on startup
     */
    private void printBanner() {
        System.out.println("========================================");
        System.out.println("  Adaptive Scheduler for Flink v1.2");
        System.out.println("  (with Real Metrics + BANDIT Support)");
        System.out.println("========================================");
        System.out.println("Configuration:");
        System.out.println("  CPU Low Threshold: " + cpuLowThreshold + "%");
        System.out.println("  CPU High Threshold: " + cpuHighThreshold + "%");
        System.out.println("  Strategy Cooldown: " + (strategyCooldownMs/1000) + "s");
        if (useFixedStrategy) {
            System.out.println("  Mode: FIXED STRATEGY (" + fixedStrategy + ")");
        } else {
            System.out.println("  Mode: ADAPTIVE");
            System.out.println("  Initial Strategy: " + currentStrategy);
        }
        System.out.println("========================================\n");
    }
    
    /**
     * Get current timestamp string
     */
    private String getCurrentTimestamp() {
        return new java.text.SimpleDateFormat("yyyy-MM-dd HH:mm:ss")
            .format(new Date());
    }
    
    /**
     * Main entry point
     */
    public static void main(String[] args) {
        try {
            AdaptiveScheduler scheduler = new AdaptiveScheduler();
            
            // Shutdown hook for statistics
            Runtime.getRuntime().addShutdownHook(new Thread(() -> {
                System.out.println("\nShutting down scheduler...");
                scheduler.printStatistics();
            }));
            
            // Run scheduler
            scheduler.run();
            
        } catch (Exception e) {
            System.err.println("FATAL ERROR: " + e.getMessage());
            e.printStackTrace();
            System.exit(1);
        }
    }
}