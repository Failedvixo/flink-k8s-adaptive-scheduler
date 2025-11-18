package com.thesis.scheduler.model;

/**
 * Types of scheduling strategies available
 */
public enum SchedulingStrategyType {
    FCFS,           // First-Come-First-Serve
    LEAST_LOADED,   // Assign to node with lowest CPU
    PRIORITY,       // Priority-based assignment
    BALANCED        // Round-robin distribution
}