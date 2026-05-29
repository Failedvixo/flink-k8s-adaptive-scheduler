package com.thesis.scheduler.strategy;

import com.google.gson.Gson;
import com.google.gson.JsonArray;
import com.google.gson.JsonObject;
import com.google.gson.JsonElement;
import com.thesis.scheduler.metrics.ClusterMetrics;
import io.kubernetes.client.openapi.models.V1Node;
import io.kubernetes.client.openapi.models.V1Pod;

import java.io.InputStream;
import java.io.InputStreamReader;
import java.util.*;
import java.util.concurrent.ConcurrentHashMap;

/**
 * Offline Contextual Bandit (LinUCB) — meta-scheduler.
 *
 * Each ARM is a base scheduling strategy (FCFS, BALANCED, SARSA).
 * Weights (theta, A_inv) per arm are pre-trained offline by
 * scripts/train_offline_bandit.py and shipped as a classpath
 * resource (offline_bandit_weights.json). At runtime the scheduler
 * does NOT update the weights — it only predicts.
 *
 * Decision flow:
 *   - Every {@link #decisionIntervalMs} ms, sample the cluster context
 *     and pick the arm with the highest LinUCB score:
 *         score(a) = theta_a · x  +  alpha * sqrt(x^T A_inv_a x)
 *   - Delegate selectNode(...) to the chosen sub-strategy.
 *
 * This is intentionally a separate strategy from the existing online
 * BANDIT (which has nodes as arms) so both can coexist for thesis
 * comparisons.
 *
 * @author Vicente (Thesis Project)
 */
public class OfflineBanditStrategy implements SchedulingStrategy {

    private static final String DEFAULT_WEIGHTS_RESOURCE = "/offline_bandit_weights.json";
    private static final String DEFAULT_LABEL = "OFFLINE_BANDIT";
    private static final String DEFAULT_ARM = "FCFS";

    private final String weightsResource;
    private final String label;
    private final Map<String, SchedulingStrategy> arms = new LinkedHashMap<>();
    private final Map<String, double[]> theta = new HashMap<>();
    private final Map<String, double[][]> aInv = new HashMap<>();
    private final List<String> featureNames;
    private final int featureDim;
    private final double alpha;
    private final long decisionIntervalMs;

    private final Map<String, Long> armSelections = new ConcurrentHashMap<>();
    private volatile String currentArm;
    private volatile long lastDecisionTime = 0L;
    private long totalDecisions = 0L;

    // State for new features (must match scripts/train_offline_bandit.py):
    //   cpu_velocity = (avg_cpu - prev_avg_cpu) / dt_sec / VELOCITY_SCALE
    //   elapsed_norm = (now - schedulerStartTime) / ELAPSED_NORM_MS
    //   saturation   = min(1.25, max_cpu / SAT_THRESHOLD)
    private static final double VELOCITY_SCALE = 10.0;
    private static final double ELAPSED_NORM_MS = 600_000.0;
    private static final double SAT_THRESHOLD = 80.0;
    private volatile long schedulerStartTime = 0L;
    private volatile double prevAvgCpu = -1.0;
    private volatile long prevContextTime = 0L;

    public OfflineBanditStrategy() {
        this(DEFAULT_WEIGHTS_RESOURCE, DEFAULT_LABEL);
    }

    public OfflineBanditStrategy(String weightsResource, String label) {
        this.weightsResource = weightsResource;
        this.label = label;
        WeightConfig cfg = loadWeights();
        this.featureNames = cfg.featureNames;
        this.featureDim = cfg.featureDim;
        this.alpha = cfg.alpha;
        this.decisionIntervalMs = cfg.decisionIntervalMs;

        for (Map.Entry<String, ArmWeights> e : cfg.arms.entrySet()) {
            String name = e.getKey();
            theta.put(name, e.getValue().theta);
            aInv.put(name, e.getValue().aInv);
            arms.put(name, instantiateBase(name));
            armSelections.put(name, 0L);
        }
        this.currentArm = arms.containsKey(DEFAULT_ARM)
                ? DEFAULT_ARM
                : arms.keySet().iterator().next();

        System.out.println("[" + label + "] Initialized LinUCB meta-scheduler");
        System.out.println("[" + label + "] Arms: " + arms.keySet());
        System.out.println("[" + label + "] Features (d=" + featureDim + "): " + featureNames);
        System.out.println("[" + label + "] alpha=" + alpha
                + " decisionIntervalMs=" + decisionIntervalMs);
        System.out.println("[" + label + "] Initial arm: " + currentArm);
    }

    @Override
    public V1Node selectNode(List<V1Node> availableNodes, V1Pod pod,
                             ClusterMetrics clusterMetrics) {
        if (availableNodes == null || availableNodes.isEmpty()) {
            System.out.println("[" + label + "] No available nodes");
            return null;
        }

        long now = System.currentTimeMillis();
        if (now - lastDecisionTime >= decisionIntervalMs || lastDecisionTime == 0L) {
            double[] x = buildContext(availableNodes, clusterMetrics);
            String chosen = chooseArm(x);
            if (!chosen.equals(currentArm)) {
                System.out.printf("[" + label + "] arm switch %s -> %s%n",
                        currentArm, chosen);
            }
            currentArm = chosen;
            lastDecisionTime = now;
            totalDecisions++;
            armSelections.merge(chosen, 1L, Long::sum);
        }

        SchedulingStrategy active = arms.get(currentArm);
        return active.selectNode(availableNodes, pod, clusterMetrics);
    }

    private double[] buildContext(List<V1Node> nodes, ClusterMetrics metrics) {
        long now = System.currentTimeMillis();
        if (schedulerStartTime == 0L) schedulerStartTime = now;

        double avgCpu = metrics.getAverageClusterCpuUsage();
        double maxCpu = 0.0;
        double minCpu = Double.POSITIVE_INFINITY;
        double sumMem = 0.0;
        int countMem = 0;
        int countCpu = 0;
        for (V1Node n : nodes) {
            double c = metrics.getNodeCpuUsage(n);
            if (c > maxCpu) maxCpu = c;
            if (c < minCpu) minCpu = c;
            countCpu++;
            double m = metrics.getNodeMemoryUsage(n);
            if (!Double.isNaN(m)) {
                sumMem += m;
                countMem++;
            }
        }
        if (countCpu == 0) minCpu = 0.0;
        double imbalance = Math.max(0.0, maxCpu - minCpu);
        double avgMem = countMem > 0 ? sumMem / countMem : 0.0;

        double velocityPerSec = 0.0;
        if (prevAvgCpu >= 0.0 && prevContextTime > 0L) {
            double dtSec = Math.max(1.0, (now - prevContextTime) / 1000.0);
            velocityPerSec = (avgCpu - prevAvgCpu) / dtSec;
        }
        prevAvgCpu = avgCpu;
        prevContextTime = now;

        double elapsedNorm = (now - schedulerStartTime) / ELAPSED_NORM_MS;
        double saturation = Math.min(1.25, maxCpu / SAT_THRESHOLD);

        double[] x = new double[featureDim];
        // Order MUST match FEATURE_NAMES in scripts/train_offline_bandit.py.
        if (featureDim >= 1) x[0] = 1.0;
        if (featureDim >= 2) x[1] = avgCpu / 100.0;
        if (featureDim >= 3) x[2] = maxCpu / 100.0;
        if (featureDim >= 4) x[3] = minCpu / 100.0;
        if (featureDim >= 5) x[4] = imbalance / 100.0;
        if (featureDim >= 6) x[5] = velocityPerSec / VELOCITY_SCALE;
        if (featureDim >= 7) x[6] = avgMem / 100.0;
        if (featureDim >= 8) x[7] = elapsedNorm;
        if (featureDim >= 9) x[8] = saturation;
        return x;
    }

    private String chooseArm(double[] x) {
        String best = currentArm;
        double bestScore = Double.NEGATIVE_INFINITY;
        StringBuilder log = new StringBuilder("[" + label + "] LinUCB scores: ");
        for (String arm : arms.keySet()) {
            double[] th = theta.get(arm);
            double[][] ainv = aInv.get(arm);
            double exploit = dot(th, x);
            double quad = quadForm(x, ainv);
            double explore = alpha * Math.sqrt(Math.max(0.0, quad));
            double score = exploit + explore;
            log.append(String.format(Locale.ROOT,
                    "%s=%.3f(exp=%.3f|var=%.3f) ", arm, score, exploit, explore));
            if (score > bestScore) {
                bestScore = score;
                best = arm;
            }
        }
        System.out.println(log.toString());
        return best;
    }

    private static double dot(double[] a, double[] b) {
        double s = 0.0;
        for (int i = 0; i < a.length; i++) s += a[i] * b[i];
        return s;
    }

    private static double quadForm(double[] x, double[][] m) {
        double s = 0.0;
        for (int i = 0; i < x.length; i++) {
            double row = 0.0;
            double[] mi = m[i];
            for (int j = 0; j < x.length; j++) row += mi[j] * x[j];
            s += x[i] * row;
        }
        return s;
    }

    private SchedulingStrategy instantiateBase(String armName) {
        switch (armName.toUpperCase(Locale.ROOT)) {
            case "FCFS":         return new FCFSStrategy();
            case "BALANCED":     return new BalancedStrategy();
            case "LEAST_LOADED": return new LeastLoadedStrategy();
            case "SARSA":        return new SarsaStrategy();
            case "BANDIT":       return new BanditStrategy();
            case "PRIORITY":     return new PriorityStrategy();
            default:
                throw new IllegalArgumentException("Unknown arm in weights: " + armName);
        }
    }

    @Override
    public String getName() {
        return label + "(" + currentArm + ")";
    }

    public String getStatisticsSummary() {
        StringBuilder sb = new StringBuilder();
        sb.append("\n========================================\n");
        sb.append("  ").append(label).append(" STATISTICS\n");
        sb.append("========================================\n");
        sb.append("Total meta-decisions: ").append(totalDecisions).append('\n');
        sb.append("Current arm: ").append(currentArm).append('\n');
        sb.append("Arm selection counts:\n");
        for (Map.Entry<String, Long> e : armSelections.entrySet()) {
            double pct = totalDecisions > 0 ? (e.getValue() * 100.0 / totalDecisions) : 0.0;
            sb.append(String.format(Locale.ROOT,
                    "  %-12s %d (%.1f%%)%n", e.getKey(), e.getValue(), pct));
        }
        sb.append("========================================\n");
        return sb.toString();
    }

    // ==========================================================
    //   Weight loading (JSON via Gson)
    // ==========================================================

    private WeightConfig loadWeights() {
        try (InputStream in = OfflineBanditStrategy.class.getResourceAsStream(weightsResource)) {
            if (in == null) {
                System.err.println("[" + label + "] WARNING: no weights at "
                        + weightsResource + " — falling back to identity model");
                return defaultWeights();
            }
            JsonObject root = new Gson().fromJson(
                    new InputStreamReader(in), JsonObject.class);
            return parseWeights(root);
        } catch (Exception e) {
            System.err.println("[" + label + "] Failed to load weights: " + e);
            return defaultWeights();
        }
    }

    private static WeightConfig parseWeights(JsonObject root) {
        WeightConfig cfg = new WeightConfig();
        cfg.featureDim = root.get("feature_dim").getAsInt();
        cfg.alpha = root.has("alpha") ? root.get("alpha").getAsDouble() : 1.0;
        cfg.decisionIntervalMs = root.has("decision_interval_ms")
                ? root.get("decision_interval_ms").getAsLong() : 30_000L;
        cfg.featureNames = new ArrayList<>();
        if (root.has("feature_names")) {
            for (JsonElement el : root.getAsJsonArray("feature_names")) {
                cfg.featureNames.add(el.getAsString());
            }
        }
        cfg.arms = new LinkedHashMap<>();
        JsonObject armsObj = root.getAsJsonObject("arms");
        for (Map.Entry<String, JsonElement> e : armsObj.entrySet()) {
            JsonObject a = e.getValue().getAsJsonObject();
            ArmWeights w = new ArmWeights();
            w.theta = toArray(a.getAsJsonArray("theta"), cfg.featureDim);
            w.aInv = toMatrix(a.getAsJsonArray("A_inv"), cfg.featureDim);
            cfg.arms.put(e.getKey(), w);
        }
        return cfg;
    }

    private static double[] toArray(JsonArray arr, int dim) {
        double[] out = new double[dim];
        for (int i = 0; i < dim && i < arr.size(); i++) out[i] = arr.get(i).getAsDouble();
        return out;
    }

    private static double[][] toMatrix(JsonArray rows, int dim) {
        double[][] out = new double[dim][dim];
        for (int i = 0; i < dim && i < rows.size(); i++) {
            JsonArray row = rows.get(i).getAsJsonArray();
            for (int j = 0; j < dim && j < row.size(); j++) {
                out[i][j] = row.get(j).getAsDouble();
            }
        }
        return out;
    }

    /** Identity / zero fallback used when no weights file is shipped. */
    private static WeightConfig defaultWeights() {
        WeightConfig cfg = new WeightConfig();
        cfg.featureDim = 9;
        cfg.alpha = 1.0;
        cfg.decisionIntervalMs = 30_000L;
        cfg.featureNames = Arrays.asList(
                "bias", "avg_cpu", "max_cpu", "min_cpu", "cpu_imbalance",
                "cpu_velocity", "avg_mem", "elapsed_norm", "saturation");
        cfg.arms = new LinkedHashMap<>();
        for (String arm : new String[]{"FCFS", "BALANCED", "SARSA"}) {
            ArmWeights w = new ArmWeights();
            w.theta = new double[cfg.featureDim];
            w.aInv = identity(cfg.featureDim);
            cfg.arms.put(arm, w);
        }
        return cfg;
    }

    private static double[][] identity(int n) {
        double[][] m = new double[n][n];
        for (int i = 0; i < n; i++) m[i][i] = 1.0;
        return m;
    }

    private static class WeightConfig {
        int featureDim;
        double alpha;
        long decisionIntervalMs;
        List<String> featureNames;
        Map<String, ArmWeights> arms;
    }

    private static class ArmWeights {
        double[] theta;
        double[][] aInv;
    }
}
