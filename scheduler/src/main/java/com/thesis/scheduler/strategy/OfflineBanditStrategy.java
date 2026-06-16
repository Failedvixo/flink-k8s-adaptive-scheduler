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
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.time.Duration;
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
    //   cpu_velocity  = (avg_cpu  - prev_avg_cpu)  / dt_sec / VELOCITY_SCALE
    //   mem_velocity  = (avg_mem  - prev_avg_mem)  / dt_sec / VELOCITY_SCALE
    //   busy_velocity = (busy_inst - prev_busy_inst) / dt_sec / VELOCITY_SCALE
    //   elapsed_norm = (now - schedulerStartTime) / ELAPSED_NORM_MS
    //   saturation   = min(1.25, max_cpu / SAT_THRESHOLD)
    private static final double VELOCITY_SCALE = 10.0;
    private static final double ELAPSED_NORM_MS = 600_000.0;
    private static final double SAT_THRESHOLD = 80.0;
    private volatile long schedulerStartTime = 0L;
    private volatile double prevAvgCpu = -1.0;
    private volatile double prevAvgMem = -1.0;
    private volatile double prevBusyInst = -1.0;
    private volatile long prevContextTime = 0L;

    // Flink REST polling for busy_inst (V5+ feature). URL configurable via
    // FLINK_REST_URL env var, default points to the JM service inside the
    // cluster. HEAVY_VERTEX_PATTERN matches the same substring used by
    // autoscaler.sh (e.g. "q2-selection", "hot-items-count", "new-users-join").
    private static final String FLINK_REST_URL_DEFAULT =
            "http://flink-jobmanager.flink.svc.cluster.local:8081";
    private static final long FLINK_CACHE_TTL_MS = 10_000L;
    private final String flinkRestUrl;
    private final String heavyVertexPattern;
    private final HttpClient httpClient;
    private final Gson restGson = new Gson();
    private volatile double cachedBusyInst = 0.0;
    private volatile long cachedBusyTime = 0L;
    private volatile String cachedJobId = null;
    private volatile String cachedVertexId = null;

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

        // Flink REST client init for busy_inst (V5+, when featureDim >= 12).
        String envUrl = System.getenv("FLINK_REST_URL");
        this.flinkRestUrl = (envUrl != null && !envUrl.isEmpty())
                ? envUrl : FLINK_REST_URL_DEFAULT;
        String envPattern = System.getenv("HEAVY_VERTEX_PATTERN");
        this.heavyVertexPattern = (envPattern != null && !envPattern.isEmpty())
                ? envPattern : "";
        this.httpClient = HttpClient.newBuilder()
                .connectTimeout(Duration.ofSeconds(5))
                .build();

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
        maybeUpdateArm(availableNodes, clusterMetrics);
        SchedulingStrategy active = arms.get(currentArm);
        return active.selectNode(availableNodes, pod, clusterMetrics);
    }

    /**
     * Called from the main AdaptiveScheduler loop every 2s. Lets the meta
     * scheduler reconsider its arm choice on a schedule even when no pods
     * are pending — so a long steady-state Nexmark job still gets decisions
     * logged and arms can react to cluster context changes.
     * Internal cooldown ({@link #decisionIntervalMs}) prevents over-firing.
     */
    public void periodicEvaluate(List<V1Node> availableNodes,
                                 ClusterMetrics clusterMetrics) {
        if (availableNodes == null || availableNodes.isEmpty()) return;
        maybeUpdateArm(availableNodes, clusterMetrics);
    }

    /**
     * Cooldown-gated arm re-evaluation. Writes a single structured line of
     * the form:
     *   [META_DECISION] [LABEL] ts=... elapsed=...s prev=ARM chosen=ARM
     *      switched=true|false features=[...] scores=[...]
     * which is easy to grep + parse from scheduler-logs.txt after the run.
     */
    private void maybeUpdateArm(List<V1Node> availableNodes,
                                ClusterMetrics clusterMetrics) {
        long now = System.currentTimeMillis();
        if (lastDecisionTime != 0L && (now - lastDecisionTime) < decisionIntervalMs) {
            return;
        }
        double[] x = buildContext(availableNodes, clusterMetrics);
        // Compute & log scores per arm (also emits the legacy [LABEL] LinUCB scores line).
        Map<String, Double> scoreByArm = new LinkedHashMap<>();
        String chosen = chooseArmWithScores(x, scoreByArm);
        String prev = currentArm;
        boolean switched = !chosen.equals(currentArm);
        if (switched) {
            System.out.printf("[" + label + "] arm switch %s -> %s%n", currentArm, chosen);
        }
        currentArm = chosen;
        lastDecisionTime = now;
        totalDecisions++;
        armSelections.merge(chosen, 1L, Long::sum);

        // Structured machine-readable line.
        double elapsedSec = (schedulerStartTime == 0L) ? 0.0
                : (now - schedulerStartTime) / 1000.0;
        StringBuilder feats = new StringBuilder("[");
        for (int i = 0; i < x.length; i++) {
            if (i > 0) feats.append(",");
            feats.append(String.format(Locale.ROOT,
                    "%s=%.4f",
                    i < featureNames.size() ? featureNames.get(i) : ("f" + i),
                    x[i]));
        }
        feats.append("]");
        StringBuilder scs = new StringBuilder("[");
        boolean first = true;
        for (Map.Entry<String, Double> e : scoreByArm.entrySet()) {
            if (!first) scs.append(",");
            scs.append(String.format(Locale.ROOT, "%s=%.4f", e.getKey(), e.getValue()));
            first = false;
        }
        scs.append("]");
        System.out.printf(Locale.ROOT,
                "[META_DECISION] [%s] ts=%d elapsed=%.1fs prev=%s chosen=%s "
                        + "switched=%s features=%s scores=%s%n",
                label, now, elapsedSec, prev, chosen,
                Boolean.toString(switched), feats, scs);
    }

    private double[] buildContext(List<V1Node> nodes, ClusterMetrics metrics) {
        long now = System.currentTimeMillis();
        if (schedulerStartTime == 0L) schedulerStartTime = now;

        double avgCpu = metrics.getAverageClusterCpuUsage();
        double maxCpu = 0.0;
        double minCpu = Double.POSITIVE_INFINITY;
        double maxMem = 0.0;
        double minMem = Double.POSITIVE_INFINITY;
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
                if (m > maxMem) maxMem = m;
                if (m < minMem) minMem = m;
            }
        }
        if (countCpu == 0) minCpu = 0.0;
        if (countMem == 0) minMem = 0.0;
        double imbalance = Math.max(0.0, maxCpu - minCpu);
        double memImbalance = Math.max(0.0, maxMem - minMem);
        double avgMem = countMem > 0 ? sumMem / countMem : 0.0;

        // CPU + mem velocity since last buildContext.
        double velocityPerSec = 0.0;
        double memVelocityPerSec = 0.0;
        if (prevAvgCpu >= 0.0 && prevContextTime > 0L) {
            double dtSec = Math.max(1.0, (now - prevContextTime) / 1000.0);
            velocityPerSec = (avgCpu - prevAvgCpu) / dtSec;
            if (prevAvgMem >= 0.0) {
                memVelocityPerSec = (avgMem - prevAvgMem) / dtSec;
            }
        }

        // busy_inst from Flink REST (cached). Skip the network call entirely
        // when the loaded weights don't use it (V1..V4 with featureDim <= 11).
        double busyInst = 0.0;
        double busyVelocityPerSec = 0.0;
        if (featureDim >= 12) {
            busyInst = fetchBusyInstCached(now);
            if (prevBusyInst >= 0.0 && prevContextTime > 0L) {
                double dtSec = Math.max(1.0, (now - prevContextTime) / 1000.0);
                busyVelocityPerSec = (busyInst - prevBusyInst) / dtSec;
            }
            prevBusyInst = busyInst;
        }

        prevAvgCpu = avgCpu;
        prevAvgMem = avgMem;
        prevContextTime = now;

        double elapsedNorm = (now - schedulerStartTime) / ELAPSED_NORM_MS;
        double saturation = Math.min(1.25, maxCpu / SAT_THRESHOLD);

        double[] x = new double[featureDim];
        // Order MUST match FEATURE_NAMES in scripts/train_offline_bandit.py.
        if (featureDim >=  1) x[0]  = 1.0;
        if (featureDim >=  2) x[1]  = avgCpu / 100.0;
        if (featureDim >=  3) x[2]  = maxCpu / 100.0;
        if (featureDim >=  4) x[3]  = minCpu / 100.0;
        if (featureDim >=  5) x[4]  = imbalance / 100.0;
        if (featureDim >=  6) x[5]  = velocityPerSec / VELOCITY_SCALE;
        if (featureDim >=  7) x[6]  = avgMem / 100.0;
        if (featureDim >=  8) x[7]  = elapsedNorm;
        if (featureDim >=  9) x[8]  = saturation;
        // V5 features:
        if (featureDim >= 10) x[9]  = memVelocityPerSec / VELOCITY_SCALE;
        if (featureDim >= 11) x[10] = memImbalance / 100.0;
        if (featureDim >= 12) x[11] = busyInst / 100.0;
        if (featureDim >= 13) x[12] = busyVelocityPerSec / VELOCITY_SCALE;
        return x;
    }

    /**
     * Cached fetch of busy_inst (heavy vertex busy % in [0, 100]) from Flink
     * REST. Returns cachedBusyInst if the cache is fresh, otherwise refreshes.
     * On any network error logs the cause to stdout and resets the cached
     * job/vertex ids so the next refresh re-discovers them.
     */
    private double fetchBusyInstCached(long now) {
        if (cachedBusyTime != 0L && (now - cachedBusyTime) < FLINK_CACHE_TTL_MS) {
            return cachedBusyInst;
        }
        try {
            double v = fetchBusyInst();
            cachedBusyInst = v;
            cachedBusyTime = now;
            return v;
        } catch (Exception e) {
            // Log so we can diagnose silent failures (DNS, wrong vertex name, etc).
            System.out.printf(
                    "[%s] busy_inst fetch failed (job=%s vertex=%s url=%s): %s%n",
                    label,
                    cachedJobId == null ? "?" : cachedJobId.substring(0, 8),
                    cachedVertexId == null ? "?" : cachedVertexId.substring(0, 8),
                    flinkRestUrl,
                    e.getMessage());
            // Invalidate cached ids so we re-discover on next call (the job
            // may have FINISHED and a fresh RUNNING one taken its place).
            cachedJobId = null;
            cachedVertexId = null;
            cachedBusyTime = now;   // avoid hammering on a hard error
            return cachedBusyInst;  // last good value, or 0
        }
    }

    /** One full round of Flink REST calls: overview → vertices → metrics. */
    private double fetchBusyInst() throws Exception {
        // Step 1: find a running job, cache the id.
        String jobId = cachedJobId;
        if (jobId == null) {
            JsonObject overview = httpGetJson("/jobs/overview");
            JsonArray jobs = overview.getAsJsonArray("jobs");
            if (jobs == null) {
                throw new RuntimeException("no 'jobs' array in /jobs/overview");
            }
            for (JsonElement el : jobs) {
                JsonObject j = el.getAsJsonObject();
                if (j.has("state") && "RUNNING".equals(j.get("state").getAsString())) {
                    jobId = j.get("jid").getAsString();
                    break;
                }
            }
            if (jobId == null) {
                // Don't throw — it's expected between cells while no job is
                // running. Just return 0 and keep ids null so next call retries.
                return 0.0;
            }
            cachedJobId = jobId;
            System.out.printf("[%s] busy_inst: discovered job %s%n",
                    label, jobId.substring(0, 8));
        }
        // Step 2: find the heavy vertex by name pattern.
        String vertexId = cachedVertexId;
        if (vertexId == null) {
            JsonObject job = httpGetJson("/jobs/" + jobId);
            JsonArray verts = job.getAsJsonArray("vertices");
            if (verts == null) {
                throw new RuntimeException("no 'vertices' in /jobs/" + jobId);
            }
            String firstName = null;
            for (JsonElement el : verts) {
                JsonObject v = el.getAsJsonObject();
                String name = v.get("name").getAsString();
                if (firstName == null) firstName = name;
                if (heavyVertexPattern.isEmpty() || name.contains(heavyVertexPattern)) {
                    vertexId = v.get("id").getAsString();
                    break;
                }
            }
            if (vertexId == null) {
                throw new RuntimeException("no vertex matched HEAVY_VERTEX_PATTERN='"
                        + heavyVertexPattern + "' (first vertex was '" + firstName + "')");
            }
            cachedVertexId = vertexId;
            System.out.printf("[%s] busy_inst: vertex matched %s (pattern='%s')%n",
                    label, vertexId.substring(0, 8), heavyVertexPattern);
        }
        // Step 3: aggregated busyTimeMsPerSecond across subtasks.
        JsonArray metrics = httpGetJsonArray(
                "/jobs/" + jobId + "/vertices/" + vertexId
                        + "/metrics?get=busyTimeMsPerSecond");
        if (metrics == null) {
            throw new RuntimeException("null metrics response");
        }
        for (JsonElement el : metrics) {
            JsonObject m = el.getAsJsonObject();
            if (m.has("id")
                    && "busyTimeMsPerSecond".equals(m.get("id").getAsString())
                    && m.has("value")) {
                double msPerSec = Double.parseDouble(m.get("value").getAsString());
                return Math.max(0.0, Math.min(100.0, msPerSec / 10.0));   // → %
            }
        }
        // Endpoint reachable but metric not in payload — could be config issue
        // on the Flink side (metrics.system.io scope disabled).
        throw new RuntimeException(
                "busyTimeMsPerSecond not in metrics response (size=" + metrics.size() + ")");
    }

    private JsonObject httpGetJson(String path) throws Exception {
        HttpRequest req = HttpRequest.newBuilder(URI.create(flinkRestUrl + path))
                .timeout(Duration.ofSeconds(5))
                .GET().build();
        HttpResponse<String> resp = httpClient.send(req, HttpResponse.BodyHandlers.ofString());
        if (resp.statusCode() / 100 != 2) {
            throw new RuntimeException("HTTP " + resp.statusCode() + " on " + path);
        }
        return restGson.fromJson(resp.body(), JsonObject.class);
    }

    private JsonArray httpGetJsonArray(String path) throws Exception {
        HttpRequest req = HttpRequest.newBuilder(URI.create(flinkRestUrl + path))
                .timeout(Duration.ofSeconds(5))
                .GET().build();
        HttpResponse<String> resp = httpClient.send(req, HttpResponse.BodyHandlers.ofString());
        if (resp.statusCode() / 100 != 2) {
            throw new RuntimeException("HTTP " + resp.statusCode() + " on " + path);
        }
        return restGson.fromJson(resp.body(), JsonArray.class);
    }

    /**
     * Compute LinUCB scores for every arm, log the legacy "LinUCB scores"
     * line, and fill {@code outScores} with arm→total-score mapping. Returns
     * the argmax arm name.
     */
    private String chooseArmWithScores(double[] x, Map<String, Double> outScores) {
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
            if (outScores != null) outScores.put(arm, score);
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
