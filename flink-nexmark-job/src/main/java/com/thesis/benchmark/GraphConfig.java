package com.thesis.benchmark;

public class GraphConfig implements java.io.Serializable {
    
    private static final long serialVersionUID = 1L;
    public int eventsPerSecond = 50000;
    public int durationSeconds = 300;
    public int globalParallelism = 4;
    public int sourceParallelism = 2;
    public int transformParallelism = 4;
    public int sinkParallelism = 2;
    
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
    
    // CPU load simulator: iterations of synthetic computation per event.
    // 0 = no extra load (default).
    public int cpuLoadIterationsPerEvent = 0;
    
    // Arrival distribution for the source.
    //   CONSTANT : fixed rate (default, same as before)
    //   STEP     : low -> high -> low phases (spike pattern)
    //   SINE     : sinusoidal variation around base rate
    public ArrivalDistribution arrivalDistribution = ArrivalDistribution.CONSTANT;
    
    // STEP distribution parameters
    // Phases: [0, stepPhase1End) at stepLowRate,
    //         [stepPhase1End, stepPhase2End) at stepHighRate,
    //         [stepPhase2End, duration) at stepLowRate
    // Rates expressed as fraction of eventsPerSecond (e.g., 0.25 = 25% of base rate)
    public double stepLowRateFraction = 0.25;   // 25% of base rate during low phases
    public double stepHighRateFraction = 1.5;    // 150% of base rate during spike
    public double stepPhase1Fraction = 0.33;     // first 33% of duration = low
    public double stepPhase2Fraction = 0.66;     // 33%-66% of duration = high, rest = low
    
    // SINE distribution parameters
    // rate(t) = eventsPerSecond * (1 + sineAmplitude * sin(2*PI*t / sinePeriodSeconds))
    // sineAmplitude should be in (0, 1) to avoid negative rates
    public double sineAmplitude = 0.7;           // ±70% variation
    public int sinePeriodSeconds = 60;            // one full cycle every 60s
    
    public enum WindowType {
        TUMBLING, SLIDING, SESSION
    }
    
    public enum AggregationType {
        SUM, AVERAGE, COUNT, MAX, MIN
    }
    
    public enum ArrivalDistribution {
        CONSTANT, STEP, SINE
    }
    
    /**
     * Compute the instantaneous event rate for this point in time.
     * @param elapsedSeconds seconds since the source started
     * @return target events per second at this moment
     */
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
        config.sourceParallelism = Math.max(1, config.globalParallelism / 2);
        config.transformParallelism = config.globalParallelism;
        config.sinkParallelism = Math.max(1, config.globalParallelism / 4);
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
        System.out.println("  Arrival dist:    " + arrivalDistribution);
        if (arrivalDistribution == ArrivalDistribution.STEP) {
            System.out.println("    Low rate:      " + (int)(eventsPerSecond * stepLowRateFraction) + " ev/s ("
                + (int)(stepLowRateFraction * 100) + "% of base)");
            System.out.println("    High rate:     " + (int)(eventsPerSecond * stepHighRateFraction) + " ev/s ("
                + (int)(stepHighRateFraction * 100) + "% of base)");
            System.out.println("    Phases:        0-" + (int)(durationSeconds * stepPhase1Fraction) + "s low, "
                + (int)(durationSeconds * stepPhase1Fraction) + "-" + (int)(durationSeconds * stepPhase2Fraction) 
                + "s HIGH, " + (int)(durationSeconds * stepPhase2Fraction) + "-" + durationSeconds + "s low");
        }
        if (arrivalDistribution == ArrivalDistribution.SINE) {
            int minRate = Math.max(1, (int)(eventsPerSecond * (1.0 - sineAmplitude)));
            int maxRate = (int)(eventsPerSecond * (1.0 + sineAmplitude));
            System.out.println("    Amplitude:     " + (int)(sineAmplitude * 100) + "%");
            System.out.println("    Period:        " + sinePeriodSeconds + "s");
            System.out.println("    Rate range:    " + minRate + " - " + maxRate + " ev/s");
        }
        System.out.println();
        System.out.println("Parallelism:");
        System.out.println("  Global:          " + globalParallelism);
        System.out.println("  Source:          " + sourceParallelism);
        System.out.println("  Transform:       " + transformParallelism);
        System.out.println("  Sink:            " + sinkParallelism);
        System.out.println();
        System.out.println("Graph Topology:");
        System.out.println("  High value filter:     " + (enableHighValueFilter ? "YES" : "NO"));
        System.out.println("  Currency conversion:   " + (enableCurrencyConversion ? "YES" : "NO"));
        System.out.println("  Auction filter:        " + (enableAuctionFilter ? "YES" : "NO"));
        System.out.println("  Bidder filter:         " + (enableBidderFilter ? "YES" : "NO"));
        System.out.println();
        System.out.println("Windowing:");
        System.out.println("  Type:            " + windowType);
        System.out.println("  Size:            " + windowSizeSeconds + "s");
        if (windowType == WindowType.SLIDING) {
            System.out.println("  Slide:           " + slideSizeSeconds + "s");
        }
        if (windowType == WindowType.SESSION) {
            System.out.println("  Gap:             " + sessionGapSeconds + "s");
        }
        System.out.println();
        System.out.println("Aggregation:");
        System.out.println("  Type:            " + aggregationType);
        System.out.println("==========================================");
        System.out.println();
    }
    
    public String getJobName() {
        String distSuffix = "";
        if (arrivalDistribution != ArrivalDistribution.CONSTANT) {
            distSuffix = ",dist=" + arrivalDistribution.name();
        }
        return String.format("Nexmark-Config[rate=%dk,par=%d,win=%s-%ds,cpu=%d%s]",
            eventsPerSecond / 1000,
            globalParallelism,
            windowType.toString().substring(0, 3),
            windowSizeSeconds,
            cpuLoadIterationsPerEvent,
            distSuffix
        );
    }
}