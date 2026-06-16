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
    OFFLINE_BANDIT_V3, // Same arms as V2 but theta trained by ProPS+ (LLM-driven policy search)
    OFFLINE_BANDIT_V4, // LinUCB ridge, arms = {FCFS, BALANCED, LEAST_LOADED, BANDIT, SARSA}, trained on q2-* (high-variance scenario)
    OFFLINE_BANDIT_V5, // LinUCB ridge with 13 features (adds mem_velocity/mem_imbalance/busy_inst/busy_velocity); arms = V4 minus SARSA
    SARSA_META         // Tabular SARSA meta-scheduler: discretised state, action = base arm; Q-table trained offline on q2-* (SARSA as meta-optimizer, not as an arm)
}