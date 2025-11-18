package com.thesis.scheduler.strategy;

import io.kubernetes.client.openapi.models.V1Node;
import io.kubernetes.client.openapi.models.V1Pod;
import com.thesis.scheduler.metrics.ClusterMetrics;

import java.util.List;

/**
 * First-Come-First-Serve (FCFS) Strategy
 * 
 * Simply assigns pods to the first available node.
 * Used when cluster load is low (<40% CPU).
 */
public class FCFSStrategy implements SchedulingStrategy {
    
    @Override
    public V1Node selectNode(List<V1Node> availableNodes, V1Pod pod, ClusterMetrics metrics) {
        if (availableNodes.isEmpty()) {
            return null;
        }
        
        // Return first available node
        return availableNodes.get(0);
    }
    
    @Override
    public String getName() {
        return "FCFS";
    }
}