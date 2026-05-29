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
    
    public enum WindowType { TUMBLING, SLIDING, SESSION }
    public enum AggregationType { SUM, AVERAGE, COUNT, MAX, MIN }
    public enum ArrivalDistribution { CONSTANT, STEP, SINE }
    
    public int getInstantRate(double elapsedSeconds) {
        switch (arrivalDistribution) {
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
     * Args: rate duration parallelism window cpuLoad arrivalDist cpuLoadParallelism maxEventAgeMs
     *        [0]   [1]       [2]      [3]    [4]       [5]           [6]               [7]
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
        
        config.sourceParallelism = Math.max(1, config.globalParallelism / 2);
        config.transformParallelism = config.globalParallelism;
        config.sinkParallelism = Math.max(1, config.globalParallelism / 4);
        if (config.cpuLoadParallelism <= 0) {
            config.cpuLoadParallelism = config.globalParallelism;
        }
        return config;
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
        System.out.println();
        System.out.println("Parallelism:");
        System.out.println("  Global:          " + globalParallelism);
        System.out.println("  Source:          " + sourceParallelism);
        System.out.println("  CPU Load:        " + cpuLoadParallelism + (cpuLoadParallelism != globalParallelism ? " (INDEPENDENT)" : ""));
        System.out.println("  Transform:       " + transformParallelism);
        System.out.println("  Sink:            " + sinkParallelism);
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
        return String.format("Nexmark-Config[rate=%dk,par=%d,win=%s-%ds,cpu=%d%s%s%s]",
            eventsPerSecond / 1000, globalParallelism,
            windowType.toString().substring(0, 3), windowSizeSeconds,
            cpuLoadIterationsPerEvent, distSuffix, ageSuffix, cpuParSuffix);
    }
}