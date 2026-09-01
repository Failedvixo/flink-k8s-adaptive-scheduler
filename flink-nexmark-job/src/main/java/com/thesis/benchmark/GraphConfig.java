package com.thesis.benchmark;

public class GraphConfig implements java.io.Serializable {
    
    private static final long serialVersionUID = 1L;
    public int eventsPerSecond = 50000;
    public int durationSeconds = 300;
    public int globalParallelism = 4;
    public int sourceParallelism = 2;
    public int transformParallelism = 4;
    public int sinkParallelism = 2;
    
    // Independent parallelism for the CPU-load operator.
    // This is the operator that gets scaled by the auto-scaler.
    // 0 means "use globalParallelism".
    public int cpuLoadParallelism = 0;
    
    public boolean enableHighValueFilter = true;
    public boolean enableCurrencyConversion = true;
    public boolean enableAuctionFilter = false;
    public boolean enableBidderFilter = false;
    
    public double minBidPrice = 100.0;
    public long maxAuctionId = 500;
    public long maxBidderId = 5000;
    
    public WindowType windowType = WindowType.TUMBLING;
    public int windowSizeSeconds = 10;
    public int slideSizeSeconds = 5;
    public int sessionGapSeconds = 30;
    
    public AggregationType aggregationType = AggregationType.SUM;
    
    public boolean enableConsoleSink = true;
    public boolean enableFileSink = false;
    public String outputPath = "/tmp/nexmark-output";
    
    public int cpuLoadIterationsPerEvent = 0;
    
    // Maximum event age in ms. Events older than this are discarded
    // at the CPU-load stage (staleness policy). 0 = disabled.
    public long maxEventAgeMs = 0;
    
    public ArrivalDistribution arrivalDistribution = ArrivalDistribution.CONSTANT;
    
    public double stepLowRateFraction = 0.25;
    public double stepHighRateFraction = 1.5;
    public double stepPhase1Fraction = 0.33;
    public double stepPhase2Fraction = 0.66;
    
    public double sineAmplitude = 0.7;
    public int sinePeriodSeconds = 60;

    // RAMP climbs linearly from this fraction of the base rate to stepHighRateFraction
    // over the whole run. Unlike STEP the load never comes back down, so the
    // autoscaler cannot recover from a bad scaling decision by waiting it out.
    public double rampStartRateFraction = 0.25;

    /**
     * How the operators are packed into slots, which decides whether placement is a
     * decision at all.
     *
     * <p>SHARED is Flink's default and what every campaign before this used: one slot
     * sharing group for the whole pipeline, so each slot holds one subtask of EVERY
     * vertex. Under it the slices are interchangeable by construction — an arm cannot
     * "put the join on the fast machine", because the join's subtasks are spread across
     * all slices along with everything else. Adding operators (Q8) does not help; it
     * makes every slice heavier and MORE alike.
     *
     * <p>PER_STAGE gives each resource profile its own group — source/filters (I/O),
     * cpu-load chain (CPU), window (state and memory), sink — so a stage becomes an
     * independently placeable unit whose machine can be chosen on its own merits.
     *
     * <p>PER_OPERATOR is the maximal version, one group per vertex. It costs the most
     * slots and the most network, and exists to bound how far the effect goes.
     *
     * <p>Breaking the sharing is not free: slots needed stop being the MAXIMUM
     * parallelism and become the SUM over groups, and data that used to stay inside one
     * slot now crosses the network. Both are the point rather than a side effect — the
     * second is what makes the communication term of the cost function do anything.
     */
    /**
     * Bytes of dead weight carried by every Bid, which is how an edge is made expensive.
     *
     * <p>Records only pay to be serialized when they cross a slot boundary into another JVM; inside
     * one slot they are handed over by reference. A 32-byte Bid makes that difference unmeasurable,
     * so the cost of splitting two communicating stages across machines stays invisible and a
     * placement policy that ignores edges — LPT does, by construction — loses nothing by ignoring
     * them. Padding the record turns serialization into real CPU on the scarce resource.
     *
     * <p>Only the Bid carries it, so exactly ONE edge is expensive: source/filters to the cpu chain.
     * Downstream of the currency conversion the records are Tuple2 and cheap again. That is
     * deliberate — one costly edge is the smallest instance in which "collect the communicating
     * operators or scatter them for compute" (SP-Ant, Farrokh et al. 2022) is a real dilemma.
     *
     * <p>0 disables it and reproduces every earlier campaign exactly.
     */
    public int payloadBytes = 0;

    public enum SlotSharingMode { SHARED, PER_STAGE, PER_OPERATOR }

    public SlotSharingMode slotSharingMode = SlotSharingMode.SHARED;

    public enum WindowType { TUMBLING, SLIDING, SESSION }
    public enum AggregationType { SUM, AVERAGE, COUNT, MAX, MIN }
    public enum ArrivalDistribution { CONSTANT, STEP, SINE, RAMP }

    public int getInstantRate(double elapsedSeconds) {
        switch (arrivalDistribution) {
            case RAMP:
                double progress = Math.min(1.0, elapsedSeconds / Math.max(1.0, durationSeconds));
                double multiplier = rampStartRateFraction
                    + (stepHighRateFraction - rampStartRateFraction) * progress;
                return Math.max(1, (int)(eventsPerSecond * multiplier));
            case STEP:
                double phase1End = durationSeconds * stepPhase1Fraction;
                double phase2End = durationSeconds * stepPhase2Fraction;
                if (elapsedSeconds < phase1End) {
                    return Math.max(1, (int)(eventsPerSecond * stepLowRateFraction));
                } else if (elapsedSeconds < phase2End) {
                    return Math.max(1, (int)(eventsPerSecond * stepHighRateFraction));
                } else {
                    return Math.max(1, (int)(eventsPerSecond * stepLowRateFraction));
                }
            case SINE:
                double factor = 1.0 + sineAmplitude 
                    * Math.sin(2.0 * Math.PI * elapsedSeconds / sinePeriodSeconds);
                return Math.max(1, (int)(eventsPerSecond * factor));
            case CONSTANT:
            default:
                return eventsPerSecond;
        }
    }
    
    /**
     * Args: rate duration parallelism window cpuLoad arrivalDist cpuLoadParallelism maxEventAgeMs slotSharing payloadBytes
     *        [0]   [1]       [2]      [3]    [4]       [5]           [6]               [7]           [8]         [9]
     *
     * Positional and not an environment variable on purpose: a misread env var would
     * silently produce a campaign that looks fine and measures the wrong graph, which
     * has already happened once here with JOB_CLASS. The mode is echoed into the job
     * NAME instead, so job-details.json records which world a run belongs to.
     */
    public static GraphConfig fromArgs(String[] args) {
        GraphConfig config = new GraphConfig();
        if (args.length > 0) config.eventsPerSecond = Integer.parseInt(args[0]);
        if (args.length > 1) config.durationSeconds = Integer.parseInt(args[1]);
        if (args.length > 2) config.globalParallelism = Integer.parseInt(args[2]);
        if (args.length > 3) config.windowSizeSeconds = Integer.parseInt(args[3]);
        if (args.length > 4) config.cpuLoadIterationsPerEvent = Integer.parseInt(args[4]);
        if (args.length > 5) {
            config.arrivalDistribution = ArrivalDistribution.valueOf(args[5].toUpperCase());
        }
        if (args.length > 6) config.cpuLoadParallelism = Integer.parseInt(args[6]);
        if (args.length > 7) config.maxEventAgeMs = Long.parseLong(args[7]);
        if (args.length > 8 && !args[8].isEmpty()) {
            config.slotSharingMode = SlotSharingMode.valueOf(args[8].toUpperCase());
        }
        if (args.length > 9 && !args[9].isEmpty()) config.payloadBytes = Integer.parseInt(args[9]);
        
        config.sourceParallelism = Math.max(1, config.globalParallelism / 2);
        config.transformParallelism = config.globalParallelism;
        config.sinkParallelism = Math.max(1, config.globalParallelism / 4);
        if (config.cpuLoadParallelism <= 0) {
            config.cpuLoadParallelism = config.globalParallelism;
        }
        return config;
    }
    
    /**
     * Slots the job needs AT SUBMISSION, which is the sum over slot sharing groups of the
     * widest vertex in each. Printed because exceeding the pool does not fail loudly — the
     * job simply sits waiting for resources that will never arrive, and a campaign burns
     * its whole schedule before anyone notices.
     *
     * <p>Only the submission-time figure: the adaptive scheduler rescales vertices
     * afterwards, so the running job's requirement moves with the parallelism it lands on.
     */
    public int slotsRequired() {
        int filters = (enableHighValueFilter || enableAuctionFilter || enableBidderFilter)
            ? globalParallelism : 0;
        switch (slotSharingMode) {
            case PER_STAGE:
                return Math.max(sourceParallelism, filters)
                    + Math.max(cpuLoadParallelism, Math.max(globalParallelism, transformParallelism))
                    + globalParallelism
                    + sinkParallelism;
            case PER_OPERATOR:
                int total = sourceParallelism + cpuLoadParallelism + globalParallelism
                    + transformParallelism + globalParallelism + sinkParallelism;
                if (enableHighValueFilter) total += globalParallelism;
                if (enableAuctionFilter) total += globalParallelism;
                if (enableBidderFilter) total += globalParallelism;
                return total;
            case SHARED:
            default:
                return Math.max(globalParallelism,
                    Math.max(sourceParallelism,
                        Math.max(cpuLoadParallelism,
                            Math.max(transformParallelism, sinkParallelism))));
        }
    }

    public void print() {
        System.out.println("==========================================");
        System.out.println("  Graph Configuration");
        System.out.println("==========================================");
        System.out.println("Workload:");
        System.out.println("  Events/sec:      " + eventsPerSecond + " (base rate)");
        System.out.println("  Duration:        " + durationSeconds + "s");
        System.out.println("  CPU load/event:  " + cpuLoadIterationsPerEvent + " iter");
        System.out.println("  Max event age:   " + (maxEventAgeMs > 0 ? maxEventAgeMs + " ms" : "DISABLED"));
        System.out.println("  Arrival dist:    " + arrivalDistribution);
        if (arrivalDistribution == ArrivalDistribution.STEP) {
            System.out.println("    Low rate:      " + (int)(eventsPerSecond * stepLowRateFraction) + " ev/s");
            System.out.println("    High rate:     " + (int)(eventsPerSecond * stepHighRateFraction) + " ev/s");
            System.out.println("    Phases:        0-" + (int)(durationSeconds * stepPhase1Fraction) + "s low, "
                + (int)(durationSeconds * stepPhase1Fraction) + "-" + (int)(durationSeconds * stepPhase2Fraction) 
                + "s HIGH, " + (int)(durationSeconds * stepPhase2Fraction) + "-" + durationSeconds + "s low");
        }
        if (arrivalDistribution == ArrivalDistribution.SINE) {
            int minRate = Math.max(1, (int)(eventsPerSecond * (1.0 - sineAmplitude)));
            int maxRate = (int)(eventsPerSecond * (1.0 + sineAmplitude));
            System.out.println("    Rate range:    " + minRate + " - " + maxRate + " ev/s");
        }
        if (arrivalDistribution == ArrivalDistribution.RAMP) {
            System.out.println("    Ramp:          "
                + Math.max(1, (int)(eventsPerSecond * rampStartRateFraction)) + " -> "
                + (int)(eventsPerSecond * stepHighRateFraction) + " ev/s, linear over "
                + durationSeconds + "s");
        }
        System.out.println();
        System.out.println("Parallelism:");
        System.out.println("  Global:          " + globalParallelism);
        System.out.println("  Source:          " + sourceParallelism);
        System.out.println("  CPU Load:        " + cpuLoadParallelism + (cpuLoadParallelism != globalParallelism ? " (INDEPENDENT)" : ""));
        System.out.println("  Transform:       " + transformParallelism);
        System.out.println("  Sink:            " + sinkParallelism);
        System.out.println();
        System.out.println("Slot sharing:      " + slotSharingMode
            + (slotSharingMode == SlotSharingMode.SHARED ? " (Flink default)" : " (BROKEN — placement per group)"));
        System.out.println("  Slots required:  " + slotsRequired() + " at submission");
        System.out.println("Record payload:    "
            + (payloadBytes > 0 ? payloadBytes + " bytes per Bid" : "none (32-byte Bid)"));
        System.out.println();
        System.out.println("Graph Topology:");
        System.out.println("  High value filter:     " + (enableHighValueFilter ? "YES" : "NO"));
        System.out.println("  Currency conversion:   " + (enableCurrencyConversion ? "YES" : "NO"));
        System.out.println();
        System.out.println("Windowing:");
        System.out.println("  Type:            " + windowType);
        System.out.println("  Size:            " + windowSizeSeconds + "s");
        System.out.println();
        System.out.println("Aggregation:       " + aggregationType);
        System.out.println("==========================================");
        System.out.println();
    }
    
    public String getJobName() {
        String distSuffix = "";
        if (arrivalDistribution != ArrivalDistribution.CONSTANT) {
            distSuffix = ",dist=" + arrivalDistribution.name();
        }
        String ageSuffix = "";
        if (maxEventAgeMs > 0) {
            ageSuffix = ",maxAge=" + maxEventAgeMs + "ms";
        }
        String cpuParSuffix = "";
        if (cpuLoadParallelism != globalParallelism) {
            cpuParSuffix = ",cpuPar=" + cpuLoadParallelism;
        }
        String payloadSuffix = payloadBytes > 0 ? ",pay=" + payloadBytes + "B" : "";
        String ssgSuffix = "";
        if (slotSharingMode != SlotSharingMode.SHARED) {
            ssgSuffix = ",ssg=" + slotSharingMode;
        }
        return String.format("Nexmark-Config[rate=%dk,par=%d,win=%s-%ds,cpu=%d%s%s%s%s%s]",
            eventsPerSecond / 1000, globalParallelism,
            windowType.toString().substring(0, 3), windowSizeSeconds,
            cpuLoadIterationsPerEvent, distSuffix, ageSuffix, cpuParSuffix, ssgSuffix, payloadSuffix);
    }
}