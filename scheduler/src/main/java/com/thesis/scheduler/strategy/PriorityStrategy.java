package com.thesis.scheduler.strategy;

import io.kubernetes.client.openapi.models.V1Node;
import io.kubernetes.client.openapi.models.V1Pod;
import com.thesis.scheduler.metrics.ClusterMetrics;

import java.util.Comparator;
import java.util.List;
import java.util.Map;

/**
 * Priority-Based Strategy
 * 
 * High-priority pods get assigned to nodes with most available resources.
 * Priority is read from pod labels.
 */
public class PriorityStrategy implements SchedulingStrategy {
    
    @Override
    public V1Node selectNode(List<V1Node> availableNodes, V1Pod pod, ClusterMetrics metrics) {
        if (availableNodes.isEmpty()) {
            return null;
        }
        
        int priority = getPodPriority(pod);
        
        if (priority >= 5) {
            // High priority: assign to best node (lowest CPU)
            return availableNodes.stream()
                .min(Comparator.comparingDouble(metrics::getNodeCpuUsage))
                .orElse(availableNodes.get(0));
        } else {
            // Low priority: assign to any available node
            return availableNodes.get(0);
        }
    }
    
    /**
     * Get pod priority from labels
     */
    private int getPodPriority(V1Pod pod) {
        Map<String, String> labels = pod.getMetadata().getLabels();
        if (labels != null && labels.containsKey("priority")) {
            try {
                return Integer.parseInt(labels.get("priority"));
            } catch (NumberFormatException e) {
                return 1;
            }
        }
        return 1;
    }
    
    @Override
    public String getName() {
        return "PRIORITY";
    }
}