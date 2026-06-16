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
 * SARSA_META — tabular SARSA meta-scheduler (Phase 5).
 *
 * Where OFFLINE_BANDIT (V1..V5) is a LinUCB meta-scheduler that scores arms
 * with a *linear* model over the continuous context, SARSA_META discretises a
 * small subset of the context into a tabular state and selects the arm with
 * the highest pre-trained Q(state, arm). It is the professor's requested
 * "SARSA as a meta-optimizer (not as an arm)" — there is no cold start because
 * the Q-table is trained fully offline (scripts/train_sarsa_meta.py) and only
 * read here.
 *
 * Decision flow (mirrors OfflineBanditStrategy so logs/plots stay compatible):
 *   - Every decisionIntervalMs, build the SAME 13-feature context vector,
 *     pick out the configured state features, bin each via the trained
 *     quantile edges into a discrete state key, and choose
 *         argmax_a Q[state][a]
 *     (falling back to the trained marginal-best arm for unseen states).
 *   - Delegate selectNode(...) to the chosen base sub-strategy.
 *
 * Kept intentionally independent from OfflineBanditStrategy (duplicating the
 * proven context/Flink-REST plumbing) so the working LinUCB strategies are not
 * touched.
 *
 * @author Vicente (Thesis Project)
 */
public class SarsaMetaStrategy implements SchedulingStrategy {

    private static final String DEFAULT_WEIGHTS_RESOURCE = "/sarsa_meta_weights.json";
    private static final String DEFAULT_LABEL = "SARSA_META";

    // MUST match FEATURE_NAMES order in scripts/train_offline_bandit.py /
    // train_sarsa_meta.py — the index of a state feature is looked up here.
    private static final String[] FEATURE_NAMES = {
            "bias", "avg_cpu", "max_cpu", "min_cpu", "cpu_imbalance",
            "cpu_velocity", "avg_mem", "elapsed_norm", "saturation",
            "mem_velocity", "mem_imbalance", "busy_inst", "busy_velocity"
    };
    private static final int FEATURE_DIM = FEATURE_NAMES.length;

    private final String weightsResource;
    private final String label;

    private final Map<String, SchedulingStrategy> arms = new LinkedHashMap<>();
    private List<String> armOrder;
    private List<String> stateFeatures;
    private int[] stateFeatureIdx;             // index of each state feature in the 13-vec
    private Map<String, double[]> binEdges;    // feature name -> inner quantile edges
    private Map<String, Map<String, Double>> qTable;  // stateKey -> arm -> Q
    private String defaultArm;
    private long decisionIntervalMs;
    private boolean needsBusyInst;             // true if a state feature uses busy_inst/_velocity

    private final Map<String, Long> armSelections = new ConcurrentHashMap<>();
    private volatile String currentArm;
    private volatile long lastDecisionTime = 0L;
    private long totalDecisions = 0L;
    private long unseenStateHits = 0L;

    // Context feature state (must match OfflineBanditStrategy / the trainer).
    private static final double VELOCITY_SCALE = 10.0;
    private static final double ELAPSED_NORM_MS = 600_000.0;
    private static final double SAT_THRESHOLD = 80.0;
    private volatile long schedulerStartTime = 0L;
    private volatile double prevAvgCpu = -1.0;
    private volatile double prevAvgMem = -1.0;
    private volatile double prevBusyInst = -1.0;
    private volatile long prevContextTime = 0L;

    // Flink REST polling for busy_inst (only used if a state feature needs it).
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

    public SarsaMetaStrategy() {
        this(DEFAULT_WEIGHTS_RESOURCE, DEFAULT_LABEL);
    }

    public SarsaMetaStrategy(String weightsResource, String label) {
        this.weightsResource = weightsResource;
        this.label = label;
        loadQTable();

        String envUrl = System.getenv("FLINK_REST_URL");
        this.flinkRestUrl = (envUrl != null && !envUrl.isEmpty())
                ? envUrl : FLINK_REST_URL_DEFAULT;
        String envPattern = System.getenv("HEAVY_VERTEX_PATTERN");
        this.heavyVertexPattern = (envPattern != null && !envPattern.isEmpty())
                ? envPattern : "";
        this.httpClient = HttpClient.newBuilder()
                .connectTimeout(Duration.ofSeconds(5))
                .build();

        for (String arm : armOrder) {
            arms.put(arm, instantiateBase(arm));
            armSelections.put(arm, 0L);
        }
        this.currentArm = (defaultArm != null && arms.containsKey(defaultArm))
                ? defaultArm : armOrder.get(0);

        System.out.println("[" + label + "] Initialized tabular SARSA meta-scheduler");
        System.out.println("[" + label + "] Arms: " + arms.keySet());
        System.out.println("[" + label + "] State features: " + stateFeatures
                + " (idx " + Arrays.toString(stateFeatureIdx) + ")");
        System.out.println("[" + label + "] States in Q-table: " + qTable.size()
                + "  default(unseen) arm: " + defaultArm
                + "  needsBusyInst: " + needsBusyInst);
        System.out.println("[" + label + "] decisionIntervalMs=" + decisionIntervalMs);
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

    /** Called from the main loop every 2s; cooldown-gated like OfflineBandit. */
    public void periodicEvaluate(List<V1Node> availableNodes,
                                 ClusterMetrics clusterMetrics) {
        if (availableNodes == null || availableNodes.isEmpty()) return;
        maybeUpdateArm(availableNodes, clusterMetrics);
    }

    private void maybeUpdateArm(List<V1Node> availableNodes,
                                ClusterMetrics clusterMetrics) {
        long now = System.currentTimeMillis();
        if (lastDecisionTime != 0L && (now - lastDecisionTime) < decisionIntervalMs) {
            return;
        }
        double[] x = buildContext(availableNodes, clusterMetrics);
        String stateKey = discretize(x);
        Map<String, Double> qByArm = qTable.get(stateKey);
        boolean seen = qByArm != null;
        if (!seen) unseenStateHits++;

        // Per-arm Q values for logging (0 for unseen so the line still parses).
        Map<String, Double> scoreByArm = new LinkedHashMap<>();
        String chosen;
        if (seen) {
            chosen = armOrder.get(0);
            double best = Double.NEGATIVE_INFINITY;
            for (String arm : armOrder) {
                double q = qByArm.getOrDefault(arm, 0.0);
                scoreByArm.put(arm, q);
                if (q > best) { best = q; chosen = arm; }
            }
        } else {
            for (String arm : armOrder) scoreByArm.put(arm, 0.0);
            chosen = defaultArm;
        }

        String prev = currentArm;
        boolean switched = !chosen.equals(currentArm);
        if (switched) {
            System.out.printf("[" + label + "] arm switch %s -> %s%n", currentArm, chosen);
        }
        currentArm = chosen;
        lastDecisionTime = now;
        totalDecisions++;
        armSelections.merge(chosen, 1L, Long::sum);

        double elapsedSec = (schedulerStartTime == 0L) ? 0.0
                : (now - schedulerStartTime) / 1000.0;
        StringBuilder feats = new StringBuilder("[");
        for (int i = 0; i < x.length; i++) {
            if (i > 0) feats.append(",");
            feats.append(String.format(Locale.ROOT, "%s=%.4f", FEATURE_NAMES[i], x[i]));
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
        // Same [META_DECISION] schema as OfflineBanditStrategy + a SARSA-specific
        // state= field so scheduler-logs.txt parsers can recover the discrete state.
        System.out.printf(Locale.ROOT,
                "[META_DECISION] [%s] ts=%d elapsed=%.1fs prev=%s chosen=%s "
                        + "switched=%s state=%s seen=%s features=%s scores=%s%n",
                label, now, elapsedSec, prev, chosen,
                Boolean.toString(switched), stateKey, Boolean.toString(seen), feats, scs);
    }

    /** Discretise the full context into a comma-joined bin-index state key. */
    private String discretize(double[] x) {
        StringBuilder sb = new StringBuilder();
        for (int k = 0; k < stateFeatures.size(); k++) {
            if (k > 0) sb.append(",");
            double v = x[stateFeatureIdx[k]];
            double[] edges = binEdges.get(stateFeatures.get(k));
            int bin = 0;
            // bisect_right: number of edges <= v (matches numpy/bisect in trainer).
            for (double e : edges) {
                if (v >= e) bin++;
            }
            sb.append(bin);
        }
        return sb.toString();
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

        double velocityPerSec = 0.0;
        double memVelocityPerSec = 0.0;
        if (prevAvgCpu >= 0.0 && prevContextTime > 0L) {
            double dtSec = Math.max(1.0, (now - prevContextTime) / 1000.0);
            velocityPerSec = (avgCpu - prevAvgCpu) / dtSec;
            if (prevAvgMem >= 0.0) {
                memVelocityPerSec = (avgMem - prevAvgMem) / dtSec;
            }
        }

        double busyInst = 0.0;
        double busyVelocityPerSec = 0.0;
        if (needsBusyInst) {
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

        double[] x = new double[FEATURE_DIM];
        x[0]  = 1.0;
        x[1]  = avgCpu / 100.0;
        x[2]  = maxCpu / 100.0;
        x[3]  = minCpu / 100.0;
        x[4]  = imbalance / 100.0;
        x[5]  = velocityPerSec / VELOCITY_SCALE;
        x[6]  = avgMem / 100.0;
        x[7]  = elapsedNorm;
        x[8]  = saturation;
        x[9]  = memVelocityPerSec / VELOCITY_SCALE;
        x[10] = memImbalance / 100.0;
        x[11] = busyInst / 100.0;
        x[12] = busyVelocityPerSec / VELOCITY_SCALE;
        return x;
    }

    // ---- Flink REST busy_inst (copied from OfflineBanditStrategy) ----

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
            System.out.printf(
                    "[%s] busy_inst fetch failed (job=%s vertex=%s url=%s): %s%n",
                    label,
                    cachedJobId == null ? "?" : cachedJobId.substring(0, 8),
                    cachedVertexId == null ? "?" : cachedVertexId.substring(0, 8),
                    flinkRestUrl,
                    e.getMessage());
            cachedJobId = null;
            cachedVertexId = null;
            cachedBusyTime = now;
            return cachedBusyInst;
        }
    }

    private double fetchBusyInst() throws Exception {
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
                return 0.0;
            }
            cachedJobId = jobId;
            System.out.printf("[%s] busy_inst: discovered job %s%n",
                    label, jobId.substring(0, 8));
        }
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
                return Math.max(0.0, Math.min(100.0, msPerSec / 10.0));
            }
        }
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

    private SchedulingStrategy instantiateBase(String armName) {
        switch (armName.toUpperCase(Locale.ROOT)) {
            case "FCFS":         return new FCFSStrategy();
            case "BALANCED":     return new BalancedStrategy();
            case "LEAST_LOADED": return new LeastLoadedStrategy();
            case "SARSA":        return new SarsaStrategy();
            case "BANDIT":       return new BanditStrategy();
            case "PRIORITY":     return new PriorityStrategy();
            default:
                throw new IllegalArgumentException("Unknown arm in Q-table: " + armName);
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
        sb.append("Unseen-state fallbacks: ").append(unseenStateHits).append('\n');
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
    //   Q-table loading (JSON via Gson)
    // ==========================================================

    private void loadQTable() {
        try (InputStream in = SarsaMetaStrategy.class.getResourceAsStream(weightsResource)) {
            if (in == null) {
                System.err.println("[" + label + "] WARNING: no Q-table at "
                        + weightsResource + " — falling back to single-arm BALANCED");
                fallbackQTable();
                return;
            }
            JsonObject root = new Gson().fromJson(new InputStreamReader(in), JsonObject.class);
            parseQTable(root);
        } catch (Exception e) {
            System.err.println("[" + label + "] Failed to load Q-table: " + e);
            fallbackQTable();
        }
    }

    private void parseQTable(JsonObject root) {
        this.armOrder = new ArrayList<>();
        for (JsonElement el : root.getAsJsonArray("arms")) {
            armOrder.add(el.getAsString());
        }
        this.stateFeatures = new ArrayList<>();
        for (JsonElement el : root.getAsJsonArray("state_features")) {
            stateFeatures.add(el.getAsString());
        }
        this.stateFeatureIdx = new int[stateFeatures.size()];
        this.needsBusyInst = false;
        for (int i = 0; i < stateFeatures.size(); i++) {
            int idx = indexOfFeature(stateFeatures.get(i));
            stateFeatureIdx[i] = idx;
            if (idx >= 11) needsBusyInst = true;   // busy_inst (11) / busy_velocity (12)
        }
        this.binEdges = new HashMap<>();
        JsonObject edgesObj = root.getAsJsonObject("bin_edges");
        for (Map.Entry<String, JsonElement> e : edgesObj.entrySet()) {
            JsonArray arr = e.getValue().getAsJsonArray();
            double[] edges = new double[arr.size()];
            for (int i = 0; i < arr.size(); i++) edges[i] = arr.get(i).getAsDouble();
            binEdges.put(e.getKey(), edges);
        }
        this.qTable = new HashMap<>();
        JsonObject qObj = root.getAsJsonObject("q");
        for (Map.Entry<String, JsonElement> e : qObj.entrySet()) {
            JsonObject armQ = e.getValue().getAsJsonObject();
            Map<String, Double> m = new LinkedHashMap<>();
            for (Map.Entry<String, JsonElement> ae : armQ.entrySet()) {
                m.put(ae.getKey(), ae.getValue().getAsDouble());
            }
            qTable.put(e.getKey(), m);
        }
        this.defaultArm = root.has("default_arm")
                ? root.get("default_arm").getAsString() : armOrder.get(0);
        this.decisionIntervalMs = root.has("decision_interval_ms")
                ? root.get("decision_interval_ms").getAsLong() : 30_000L;
    }

    private static int indexOfFeature(String name) {
        for (int i = 0; i < FEATURE_NAMES.length; i++) {
            if (FEATURE_NAMES[i].equals(name)) return i;
        }
        throw new IllegalArgumentException("Unknown state feature: " + name);
    }

    /** Degenerate single-arm table used when no Q-table resource is shipped. */
    private void fallbackQTable() {
        this.armOrder = new ArrayList<>(Collections.singletonList("BALANCED"));
        this.stateFeatures = new ArrayList<>();
        this.stateFeatureIdx = new int[0];
        this.binEdges = new HashMap<>();
        this.qTable = new HashMap<>();
        this.defaultArm = "BALANCED";
        this.decisionIntervalMs = 30_000L;
        this.needsBusyInst = false;
    }
}
