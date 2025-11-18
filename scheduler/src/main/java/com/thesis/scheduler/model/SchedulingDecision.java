package com.thesis.scheduler.model;

/**
 * Records a scheduling decision for analysis
 */
public class SchedulingDecision {
    
    private final String podName;
    private final String nodeName;
    private final SchedulingStrategyType strategy;
    private final double nodeCpuUsage;
    private final long timestamp;
    
    public SchedulingDecision(String podName, String nodeName, 
                             SchedulingStrategyType strategy,
                             double nodeCpuUsage, long timestamp) {
        this.podName = podName;
        this.nodeName = nodeName;
        this.strategy = strategy;
        this.nodeCpuUsage = nodeCpuUsage;
        this.timestamp = timestamp;
    }
    
    // Getters
    public String getPodName() {
        return podName;
    }
    
    public String getNodeName() {
        return nodeName;
    }
    
    public SchedulingStrategyType getStrategy() {
        return strategy;
    }
    
    public double getNodeCpuUsage() {
        return nodeCpuUsage;
    }
    
    public long getTimestamp() {
        return timestamp;
    }
    
    @Override
    public String toString() {
        return String.format("SchedulingDecision{pod='%s', node='%s', strategy=%s, cpu=%.1f%%, time=%d}",
            podName, nodeName, strategy, nodeCpuUsage, timestamp);
    }
}