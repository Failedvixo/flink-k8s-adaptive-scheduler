package com.thesis.scheduler.strategy;

import io.kubernetes.client.openapi.models.V1Node;
import io.kubernetes.client.openapi.models.V1Pod;
import com.thesis.scheduler.metrics.ClusterMetrics;

import java.util.Comparator;
import java.util.List;

/**
 * Least-Loaded Strategy
 * 
 * Assigns pods to the node with lowest CPU usage.
 * Used when cluster load is high (>80% CPU).
 */
public class LeastLoadedStrategy implements SchedulingStrategy {
    
    @Override
    public V1Node selectNode(List<V1Node> availableNodes, V1Pod pod, ClusterMetrics metrics) {
        if (availableNodes.isEmpty()) {
            return null;
        }
        
        // Find node with minimum CPU usage
        return availableNodes.stream()
            .min(Comparator.comparingDouble(metrics::getNodeCpuUsage))
            .orElse(availableNodes.get(0));
    }
    
    @Override
    public String getName() {
        return "LEAST_LOADED";
    }
}