package com.thesis.scheduler.strategy;

import io.kubernetes.client.openapi.models.V1Node;
import io.kubernetes.client.openapi.models.V1Pod;
import com.thesis.scheduler.metrics.ClusterMetrics;

import java.util.List;

/**
 * Interface for scheduling strategies
 * 
 * Each strategy implements a different algorithm for selecting
 * which node should run a given pod.
 */
public interface SchedulingStrategy {
    
    /**
     * Select the best node for the given pod
     * 
     * @param availableNodes List of nodes available for scheduling
     * @param pod The pod to be scheduled
     * @param metrics Cluster metrics for decision-making
     * @return The selected node, or null if none suitable
     */
    V1Node selectNode(List<V1Node> availableNodes, V1Pod pod, ClusterMetrics metrics);
    
    /**
     * Get the name of this strategy
     */
    String getName();
}