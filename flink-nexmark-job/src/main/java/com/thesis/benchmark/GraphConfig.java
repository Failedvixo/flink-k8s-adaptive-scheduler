package com.thesis.benchmark;

public class GraphConfig {
    
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
    
    public enum WindowType {
        TUMBLING, SLIDING, SESSION
    }
    
    public enum AggregationType {
        SUM, AVERAGE, COUNT, MAX, MIN
    }
    
    public static GraphConfig fromArgs(String[] args) {
        GraphConfig config = new GraphConfig();
        if (args.length > 0) config.eventsPerSecond = Integer.parseInt(args[0]);
        if (args.length > 1) config.durationSeconds = Integer.parseInt(args[1]);
        if (args.length > 2) config.globalParallelism = Integer.parseInt(args[2]);
        if (args.length > 3) config.windowSizeSeconds = Integer.parseInt(args[3]);
        config.sourceParallelism = Math.max(1, config.globalParallelism / 2);
        return config;
    }
    
    public void print() {
        System.out.println("==========================================");
        System.out.println("  Graph Configuration");
        System.out.println("==========================================");
        System.out.println("Workload:");
        System.out.println("  Events/sec:      " + eventsPerSecond);
        System.out.println("  Duration:        " + durationSeconds + "s");
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
        return String.format("Nexmark-Config[rate=%dk,par=%d,win=%s-%ds]",
            eventsPerSecond / 1000,
            globalParallelism,
            windowType.toString().substring(0, 3),
            windowSizeSeconds
        );
    }
}
