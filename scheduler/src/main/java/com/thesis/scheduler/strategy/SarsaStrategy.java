package com.thesis.scheduler.strategy;

import com.thesis.scheduler.metrics.ClusterMetrics;
import io.kubernetes.client.openapi.models.V1Node;
import io.kubernetes.client.openapi.models.V1Pod;

import java.util.*;
import java.util.concurrent.ConcurrentHashMap;

/**
 * SARSA (State-Action-Reward-State-Action) on-policy reinforcement learning
 * strategy for pod scheduling.
 *
 * Unlike the Bandit (UCB1) strategy which is stateless, SARSA models the
 * environment as an MDP:
 *   - State  s : discretized (cluster_load_level, hottest_node_load_level)
 *   - Action a : index of the chosen node
 *   - Reward r : same shape as Bandit (optimal CPU range = high reward)
 *   - Next s'  : observed after the placement
 *   - Next a'  : action selected at s' under the same epsilon-greedy policy
 *
 * Update rule (on-policy):
 *   Q(s,a) <- Q(s,a) + alpha * [ r + gamma * Q(s',a') - Q(s,a) ]
 *
 * Exploration: epsilon-greedy with decay based on state-visit count.
 *
 * This is intentionally implemented in the same code style as BanditStrategy
 * so that experimental comparisons in the thesis are fair.
 *
 * @author Vicente (Thesis Project)
 */
public class SarsaStrategy implements SchedulingStrategy {

    // -------- Hyperparameters --------
    private static final double ALPHA = 0.2;          // learning rate
    private static final double GAMMA = 0.8;          // discount factor
    private static final double EPSILON_MIN = 0.05;   // floor for exploration
    private static final double INITIAL_Q = 0.5;      // optimistic init

    // CPU buckets (must match reward function ranges to be meaningful)
    private static final double LOW_CPU  = 30.0;
    private static final double HIGH_CPU = 70.0;

    // Optimal CPU range for the reward function (same as Bandit)
    private static final double OPTIMAL_CPU_LOW  = 30.0;
    private static final double OPTIMAL_CPU_HIGH = 70.0;

    // -------- Q-table and bookkeeping --------
    // Key: "stateKey|action" -> Q-value
    private final Map<String, Double> qTable = new ConcurrentHashMap<>();
    // Visits per state for epsilon decay
    private final Map<String, Long> stateVisits = new ConcurrentHashMap<>();
    // Selections per node (for logging, parity with Bandit)
    private final Map<String, Long> nodeSelections = new ConcurrentHashMap<>();

    // Pending SARSA tuple: we need (s,a,r,s') and then a' to update Q(s,a)
    private String  prevStateKey = null;
    private Integer prevAction   = null;
    private Double  prevReward   = null;

    private long totalSelections = 0;
    private final Random random = new Random();

    private final List<SarsaRecord> history =
            Collections.synchronizedList(new ArrayList<>());

    public SarsaStrategy() {
        System.out.println("[SARSA] Initialized SARSA Strategy (on-policy TD control)");
        System.out.println("[SARSA] alpha=" + ALPHA + " gamma=" + GAMMA
                + " epsilon_min=" + EPSILON_MIN);
        System.out.println("[SARSA] Optimal CPU range: " + OPTIMAL_CPU_LOW
                + "% - " + OPTIMAL_CPU_HIGH + "%");
    }

    @Override
    public V1Node selectNode(List<V1Node> availableNodes, V1Pod pod,
                             ClusterMetrics clusterMetrics) {
        if (availableNodes == null || availableNodes.isEmpty()) {
            System.out.println("[SARSA] No available nodes for scheduling");
            return null;
        }

        Map<String, Double> nodeCpuUsage = clusterMetrics.getNodeCpuUsage();

        // 1. Build current state s'
        String currentState = buildStateKey(availableNodes, nodeCpuUsage);
        stateVisits.merge(currentState, 1L, Long::sum);

        // 2. Choose action a' from s' using epsilon-greedy (this is the
        //    "next action" SARSA needs)
        int actionIdx = epsilonGreedyAction(currentState, availableNodes);
        V1Node chosen = availableNodes.get(actionIdx);
        String chosenName = chosen.getMetadata().getName();
        double cpuAtSelection = nodeCpuUsage.getOrDefault(chosenName, 50.0);

        // 3. Compute the reward r for the previous transition (we observe it now)
        double rewardForCurrent = calculateReward(cpuAtSelection);

        // 4. SARSA update for the previous (s,a) using observed r and Q(s',a')
        if (prevStateKey != null && prevAction != null && prevReward != null) {
            double qNext = qValue(currentState, actionIdx);
            double oldQ  = qValue(prevStateKey, prevAction);
            double newQ  = oldQ + ALPHA * (prevReward + GAMMA * qNext - oldQ);
            qTable.put(qKey(prevStateKey, prevAction), newQ);

            System.out.println(String.format(
                "[SARSA] [UPDATE] s=%s a=%d r=%.3f s'=%s a'=%d  Q: %.4f -> %.4f",
                prevStateKey, prevAction, prevReward,
                currentState, actionIdx, oldQ, newQ));
        }

        // 5. Shift window: current becomes "previous" for the next call
        prevStateKey = currentState;
        prevAction   = actionIdx;
        prevReward   = rewardForCurrent;

        // 6. Bookkeeping
        totalSelections++;
        nodeSelections.merge(chosenName, 1L, Long::sum);

        SarsaRecord rec = new SarsaRecord(
                chosenName, System.currentTimeMillis(),
                cpuAtSelection, currentState, actionIdx,
                rewardForCurrent, qValue(currentState, actionIdx));
        history.add(rec);

        System.out.println(String.format(
            "[SARSA] [SELECTION] Node: %s | State: %s | Action: %d | CPU: %.1f%% "
                + "| Reward: %.3f | Q(s,a)=%.4f | Total: %d",
            chosenName, currentState, actionIdx, cpuAtSelection,
            rewardForCurrent, qValue(currentState, actionIdx), totalSelections));

        return chosen;
    }

    // -------- State / Action helpers --------

    /**
     * State = (cluster_avg_bucket, hottest_node_bucket).
     * Buckets: LOW (<30%), MED (30-70%), HIGH (>70%).
     * 9 possible states total.
     */
    private String buildStateKey(List<V1Node> nodes, Map<String, Double> cpu) {
        double sum = 0.0;
        double hottest = 0.0;
        int n = 0;
        for (V1Node node : nodes) {
            String name = node.getMetadata().getName();
            double c = cpu.getOrDefault(name, 50.0);
            sum += c;
            if (c > hottest) hottest = c;
            n++;
        }
        double avg = n > 0 ? sum / n : 0.0;
        return bucket(avg) + "_" + bucket(hottest);
    }

    private String bucket(double cpu) {
        if (cpu < LOW_CPU) return "LOW";
        if (cpu <= HIGH_CPU) return "MED";
        return "HIGH";
    }

    private int epsilonGreedyAction(String stateKey, List<V1Node> nodes) {
        long visits = stateVisits.getOrDefault(stateKey, 1L);
        double epsilon = Math.max(EPSILON_MIN, 1.0 / Math.sqrt(visits));

        if (random.nextDouble() < epsilon) {
            int a = random.nextInt(nodes.size());
            System.out.println(String.format(
                "[SARSA] [EXPLORE] state=%s eps=%.3f action=%d",
                stateKey, epsilon, a));
            return a;
        }

        // Greedy: argmax_a Q(s,a). Break ties randomly.
        double bestQ = Double.NEGATIVE_INFINITY;
        List<Integer> bestActions = new ArrayList<>();
        for (int i = 0; i < nodes.size(); i++) {
            double q = qValue(stateKey, i);
            if (q > bestQ) {
                bestQ = q;
                bestActions.clear();
                bestActions.add(i);
            } else if (q == bestQ) {
                bestActions.add(i);
            }
        }
        int chosen = bestActions.get(random.nextInt(bestActions.size()));
        System.out.println(String.format(
            "[SARSA] [EXPLOIT] state=%s eps=%.3f action=%d Q=%.4f",
            stateKey, epsilon, chosen, bestQ));
        return chosen;
    }

    private double qValue(String stateKey, int action) {
        return qTable.getOrDefault(qKey(stateKey, action), INITIAL_Q);
    }

    private String qKey(String stateKey, int action) {
        return stateKey + "|" + action;
    }

    // -------- Reward (same shape as Bandit, for fair comparison) --------

    private double calculateReward(double cpuUsage) {
        double reward;
        if (cpuUsage >= OPTIMAL_CPU_LOW && cpuUsage <= OPTIMAL_CPU_HIGH) {
            reward = 1.0;
        } else if (cpuUsage < OPTIMAL_CPU_LOW) {
            reward = 0.5 + (cpuUsage / OPTIMAL_CPU_LOW) * 0.3;
        } else if (cpuUsage <= 90.0) {
            reward = 1.0 - ((cpuUsage - OPTIMAL_CPU_HIGH) / (90.0 - OPTIMAL_CPU_HIGH)) * 0.5;
        } else {
            reward = 0.2;
        }
        return Math.max(0.0, Math.min(1.0, reward));
    }

    // -------- Public utilities --------

    public String getStatisticsSummary() {
        StringBuilder sb = new StringBuilder();
        sb.append("\n========================================\n");
        sb.append("  SARSA STATISTICS SUMMARY\n");
        sb.append("========================================\n");
        sb.append(String.format("Total selections: %d%n", totalSelections));
        sb.append(String.format("Distinct states visited: %d%n", stateVisits.size()));
        sb.append(String.format("Q-table entries: %d%n", qTable.size()));

        sb.append("\nPer-node selections:\n");
        for (Map.Entry<String, Long> e : nodeSelections.entrySet()) {
            double pct = totalSelections > 0
                    ? (e.getValue() * 100.0 / totalSelections) : 0.0;
            sb.append(String.format("  %s: %d (%.1f%%)%n",
                    e.getKey(), e.getValue(), pct));
        }

        sb.append("\nTop Q-values:\n");
        qTable.entrySet().stream()
                .sorted((a, b) -> Double.compare(b.getValue(), a.getValue()))
                .limit(10)
                .forEach(e -> sb.append(String.format("  %s = %.4f%n",
                        e.getKey(), e.getValue())));
        sb.append("========================================\n");
        return sb.toString();
    }

    public void reset() {
        qTable.clear();
        stateVisits.clear();
        nodeSelections.clear();
        prevStateKey = null;
        prevAction   = null;
        prevReward   = null;
        totalSelections = 0;
        history.clear();
        System.out.println("[SARSA] State reset");
    }

    public long getTotalSelections() { return totalSelections; }
    public Map<String, Double> getQTable() { return new HashMap<>(qTable); }
    public List<SarsaRecord> getHistory() { return new ArrayList<>(history); }

    @Override
    public String getName() {
        return "SARSA";
    }

    // -------- Inner record class --------

    public static class SarsaRecord {
        public final String nodeName;
        public final long timestamp;
        public final double cpuAtSelection;
        public final String stateKey;
        public final int actionIdx;
        public final double reward;
        public final double qValue;

        public SarsaRecord(String nodeName, long timestamp, double cpuAtSelection,
                           String stateKey, int actionIdx,
                           double reward, double qValue) {
            this.nodeName = nodeName;
            this.timestamp = timestamp;
            this.cpuAtSelection = cpuAtSelection;
            this.stateKey = stateKey;
            this.actionIdx = actionIdx;
            this.reward = reward;
            this.qValue = qValue;
        }

        @Override
        public String toString() {
            return String.format(
                "SarsaRecord{node=%s state=%s action=%d cpu=%.1f%% r=%.3f Q=%.4f}",
                nodeName, stateKey, actionIdx, cpuAtSelection, reward, qValue);
        }
    }
}
