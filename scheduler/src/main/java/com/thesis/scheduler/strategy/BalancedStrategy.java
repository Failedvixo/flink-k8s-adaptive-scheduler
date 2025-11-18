package com.thesis.scheduler.strategy;

import io.kubernetes.client.openapi.models.V1Node;
import io.kubernetes.client.openapi.models.V1Pod;
import com.thesis.scheduler.metrics.ClusterMetrics;

import java.util.List;

/**
 * Balanced Strategy
 * 
 * Distributes pods evenly across nodes using round-robin.
 * Used when cluster load is medium (40-80% CPU).
 */
public class BalancedStrategy implements SchedulingStrategy {
    
    private static int roundRobinCounter = 0;
    
    @Override
    public V1Node selectNode(List<V1Node> availableNodes, V1Pod pod, ClusterMetrics metrics) {
        if (availableNodes.isEmpty()) {
            return null;
        }
        
        // Round-robin selection
        int index = roundRobinCounter % availableNodes.size();
        roundRobinCounter++;
        
        return availableNodes.get(index);
    }
    
    @Override
    public String getName() {
        return "BALANCED";
    }
}