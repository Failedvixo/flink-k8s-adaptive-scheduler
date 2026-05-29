package com.thesis.scheduler.model;

/**
 * Types of scheduling strategies available
 */
public enum SchedulingStrategyType {
    FCFS,           // First-Come-First-Serve
    LEAST_LOADED,   // Assign to node with lowest CPU
    PRIORITY,       // Priority-based assignment
    BALANCED,        // Round-robin distribution
    BANDIT,
    SARSA,
    OFFLINE_BANDIT,    // Contextual bandit (LinUCB), arms = {FCFS, BALANCED, SARSA}
    OFFLINE_BANDIT_V2, // LinUCB with arms = {BANDIT, LEAST_LOADED, BALANCED}
    OFFLINE_BANDIT_V3  // Same arms as V2 but theta trained by ProPS+ (LLM-driven policy search)
}