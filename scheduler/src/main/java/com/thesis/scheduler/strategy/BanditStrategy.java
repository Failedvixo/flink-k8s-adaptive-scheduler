package com.thesis.scheduler.strategy;

import com.thesis.scheduler.metrics.ClusterMetrics;
import io.kubernetes.client.openapi.models.V1Node;
import io.kubernetes.client.openapi.models.V1Pod;

import java.util.*;
import java.util.concurrent.ConcurrentHashMap;

/**
 * Multi-Armed Bandit Strategy using UCB1 (Upper Confidence Bound) algorithm.
 * 
 * Each node is treated as an "arm" of the bandit. The algorithm balances:
 * - Exploitation: Choosing nodes that have performed well historically
 * - Exploration: Trying nodes that haven't been selected often
 * 
 * The reward is based on:
 * - CPU utilization efficiency (not too high, not too low)
 * - Successful pod scheduling
 * - Resource availability
 * 
 * UCB1 Formula: Q(a) + c * sqrt(ln(N) / n(a))
 * Where:
 * - Q(a) = average reward for arm a
 * - N = total number of selections
 * - n(a) = number of times arm a was selected
 * - c = exploration parameter (typically sqrt(2))
 * 
 * @author Vicente (Thesis Project)
 */
public class BanditStrategy implements SchedulingStrategy {
    
    // UCB1 exploration parameter (sqrt(2) is theoretically optimal)
    private static final double EXPLORATION_PARAM = Math.sqrt(2);
    
    // Minimum selections before UCB kicks in (ensures all arms are tried)
    private static final int MIN_SELECTIONS_PER_ARM = 2;
    
    // Target CPU utilization range for optimal reward
    private static final double OPTIMAL_CPU_LOW = 30.0;
    private static final double OPTIMAL_CPU_HIGH = 70.0;
    
    // Statistics per node (arm)
    private final Map<String, ArmStatistics> armStats = new ConcurrentHashMap<>();
    
    // Total number of selections across all arms
    private long totalSelections = 0;
    
    // History for analysis
    private final List<SelectionRecord> selectionHistory = Collections.synchronizedList(new ArrayList<>());
    
    public BanditStrategy() {
        System.out.println("[BANDIT] Initialized Multi-Armed Bandit Strategy with UCB1");
        System.out.println("[BANDIT] Exploration parameter: " + EXPLORATION_PARAM);
        System.out.println("[BANDIT] Optimal CPU range: " + OPTIMAL_CPU_LOW + "% - " + OPTIMAL_CPU_HIGH + "%");
    }
    
    @Override
    public V1Node selectNode(List<V1Node> availableNodes, V1Pod pod, ClusterMetrics clusterMetrics) {
        if (availableNodes == null || availableNodes.isEmpty()) {
            System.out.println("[BANDIT] No available nodes for scheduling");
            return null;
        }
        
        // Get current CPU usage per node (REAL metrics)
        Map<String, Double> nodeCpuUsage = clusterMetrics.getNodeCpuUsage();
        
        // Initialize arms for new nodes
        for (V1Node node : availableNodes) {
            String nodeName = node.getMetadata().getName();
            armStats.putIfAbsent(nodeName, new ArmStatistics(nodeName));
        }
        
        // Phase 1: Ensure minimum exploration (try each arm at least MIN_SELECTIONS_PER_ARM times)
        for (V1Node node : availableNodes) {
            String nodeName = node.getMetadata().getName();
            ArmStatistics stats = armStats.get(nodeName);
            if (stats.getSelectionCount() < MIN_SELECTIONS_PER_ARM) {
                System.out.println("[BANDIT] Exploration phase: selecting under-explored node " + nodeName);
                return selectAndRecord(node, nodeCpuUsage, "EXPLORATION");
            }
        }
        
        // Phase 2: UCB1 selection
        V1Node bestNode = null;
        double bestUcbValue = Double.NEGATIVE_INFINITY;
        
        StringBuilder ucbLog = new StringBuilder("\n[BANDIT] UCB1 Values:\n");
        
        for (V1Node node : availableNodes) {
            String nodeName = node.getMetadata().getName();
            ArmStatistics stats = armStats.get(nodeName);
            double ucbValue = calculateUCB1(stats);
            
            // Get current CPU for context (REAL from Metrics Server)
            double currentCpu = nodeCpuUsage.getOrDefault(nodeName, 50.0);
            
            ucbLog.append(String.format("  %s: UCB=%.4f (Q=%.4f, n=%d, CPU=%.1f%%)\n",
                nodeName, ucbValue, stats.getAverageReward(), 
                stats.getSelectionCount(), currentCpu));
            
            if (ucbValue > bestUcbValue) {
                bestUcbValue = ucbValue;
                bestNode = node;
            }
        }
        
        System.out.println(ucbLog.toString());
        
        if (bestNode != null) {
            System.out.println(String.format("[BANDIT] UCB1 selected node %s with UCB value %.4f", 
                bestNode.getMetadata().getName(), bestUcbValue));
            return selectAndRecord(bestNode, nodeCpuUsage, "UCB1");
        }
        
        // Fallback: random selection
        System.out.println("[BANDIT] Fallback to random selection");
        V1Node randomNode = availableNodes.get(new Random().nextInt(availableNodes.size()));
        return selectAndRecord(randomNode, nodeCpuUsage, "RANDOM");
    }
    
    /**
     * Calculate UCB1 value for an arm.
     * UCB1 = Q(a) + c * sqrt(ln(N) / n(a))
     */
    private double calculateUCB1(ArmStatistics stats) {
        if (stats.getSelectionCount() == 0) {
            return Double.MAX_VALUE; // Ensure unselected arms are tried
        }
        
        double exploitation = stats.getAverageReward();
        double exploration = EXPLORATION_PARAM * 
            Math.sqrt(Math.log(totalSelections + 1) / stats.getSelectionCount());
        
        return exploitation + exploration;
    }
    
    /**
     * Record selection and update statistics.
     */
    private V1Node selectAndRecord(V1Node node, Map<String, Double> nodeCpuUsage, String reason) {
        String nodeName = node.getMetadata().getName();
        double cpuBefore = nodeCpuUsage.getOrDefault(nodeName, 50.0);
        
        // Increment selection count
        totalSelections++;
        ArmStatistics stats = armStats.get(nodeName);
        stats.incrementSelection();
        
        // Calculate immediate reward based on current CPU
        double reward = calculateReward(cpuBefore, true);
        stats.updateReward(reward);
        
        // Record for history
        SelectionRecord record = new SelectionRecord(
            nodeName, 
            System.currentTimeMillis(),
            cpuBefore,
            reason,
            stats.getAverageReward()
        );
        selectionHistory.add(record);
        
        // Log selection
        System.out.println(String.format(
            "[BANDIT] [SELECTION] Node: %s | Reason: %s | CPU: %.1f%% | Reward: %.4f | Total: %d | Arm: %d",
            nodeName, reason, cpuBefore, reward, totalSelections, stats.getSelectionCount()));
        
        return node;
    }
    
    /**
     * Calculate reward based on CPU utilization and success.
     * 
     * Reward function:
     * - Optimal range (30-70%): reward = 1.0
     * - Under-utilized (<30%): moderate reward
     * - High load (70-90%): decreasing reward
     * - Overloaded (>90%): low reward
     */
    private double calculateReward(double cpuUsage, boolean success) {
        if (!success) {
            return 0.0;
        }
        
        double reward;
        
        if (cpuUsage >= OPTIMAL_CPU_LOW && cpuUsage <= OPTIMAL_CPU_HIGH) {
            // Optimal range: high reward
            reward = 1.0;
        } else if (cpuUsage < OPTIMAL_CPU_LOW) {
            // Under-utilized: moderate reward (still usable)
            reward = 0.5 + (cpuUsage / OPTIMAL_CPU_LOW) * 0.3;
        } else if (cpuUsage <= 90.0) {
            // High but acceptable: decreasing reward
            reward = 1.0 - ((cpuUsage - OPTIMAL_CPU_HIGH) / (90.0 - OPTIMAL_CPU_HIGH)) * 0.5;
        } else {
            // Overloaded: low reward
            reward = 0.2;
        }
        
        return Math.max(0.0, Math.min(1.0, reward));
    }
    
    /**
     * Get statistics summary for logging/debugging.
     */
    public String getStatisticsSummary() {
        StringBuilder sb = new StringBuilder();
        sb.append("\n========================================\n");
        sb.append("  BANDIT STATISTICS SUMMARY\n");
        sb.append("========================================\n");
        sb.append(String.format("Total selections: %d\n", totalSelections));
        sb.append("\nPer-node statistics:\n");
        
        for (Map.Entry<String, ArmStatistics> entry : armStats.entrySet()) {
            ArmStatistics stats = entry.getValue();
            sb.append(String.format("  %s:\n", entry.getKey()));
            sb.append(String.format("    Selections: %d (%.1f%%)\n", 
                stats.getSelectionCount(),
                totalSelections > 0 ? (stats.getSelectionCount() * 100.0 / totalSelections) : 0));
            sb.append(String.format("    Avg Reward: %.4f\n", stats.getAverageReward()));
            sb.append(String.format("    Total Reward: %.4f\n", stats.getTotalReward()));
        }
        sb.append("========================================\n");
        
        return sb.toString();
    }
    
    /**
     * Reset all statistics (useful for experiments).
     */
    public void reset() {
        armStats.clear();
        totalSelections = 0;
        selectionHistory.clear();
        System.out.println("[BANDIT] Statistics reset");
    }
    
    /**
     * Get selection history for analysis.
     */
    public List<SelectionRecord> getSelectionHistory() {
        return new ArrayList<>(selectionHistory);
    }
    
    /**
     * Get total selections count.
     */
    public long getTotalSelections() {
        return totalSelections;
    }
    
    /**
     * Get arm statistics map.
     */
    public Map<String, ArmStatistics> getArmStats() {
        return new HashMap<>(armStats);
    }
    
    @Override
    public String getName() {
        return "BANDIT";
    }
    
    // ==========================================
    // Inner Classes
    // ==========================================
    
    /**
     * Statistics for a single arm (node).
     */
    public static class ArmStatistics {
        private final String nodeName;
        private long selectionCount = 0;
        private double totalReward = 0.0;
        private long lastSelectionTime = 0;
        
        public ArmStatistics(String nodeName) {
            this.nodeName = nodeName;
        }
        
        public synchronized void incrementSelection() {
            selectionCount++;
            lastSelectionTime = System.currentTimeMillis();
        }
        
        public synchronized void updateReward(double reward) {
            totalReward += reward;
        }
        
        public synchronized long getSelectionCount() {
            return selectionCount;
        }
        
        public synchronized double getTotalReward() {
            return totalReward;
        }
        
        public synchronized double getAverageReward() {
            return selectionCount > 0 ? totalReward / selectionCount : 0.0;
        }
        
        public synchronized boolean wasRecentlySelected(long windowMs) {
            return System.currentTimeMillis() - lastSelectionTime < windowMs;
        }
        
        public String getNodeName() {
            return nodeName;
        }
    }
    
    /**
     * Record of a selection for history tracking.
     */
    public static class SelectionRecord {
        public final String nodeName;
        public final long timestamp;
        public final double cpuAtSelection;
        public final String reason;
        public final double avgRewardAtSelection;
        
        public SelectionRecord(String nodeName, long timestamp, double cpuAtSelection, 
                              String reason, double avgRewardAtSelection) {
            this.nodeName = nodeName;
            this.timestamp = timestamp;
            this.cpuAtSelection = cpuAtSelection;
            this.reason = reason;
            this.avgRewardAtSelection = avgRewardAtSelection;
        }
        
        @Override
        public String toString() {
            return String.format("SelectionRecord{node=%s, cpu=%.1f%%, reason=%s, avgReward=%.4f}",
                nodeName, cpuAtSelection, reason, avgRewardAtSelection);
        }
    }
}