package com.thesis.benchmark;

import org.apache.flink.api.common.functions.FlatMapFunction;
import org.apache.flink.api.java.tuple.Tuple2;
import org.apache.flink.streaming.api.datastream.DataStream;
import org.apache.flink.streaming.api.environment.StreamExecutionEnvironment;
import org.apache.flink.streaming.api.functions.source.SourceFunction;
import org.apache.flink.util.Collector;

import java.util.Random;

/**
 * Nexmark-inspired Benchmark Job for Adaptive Scheduler Testing
 * 
 * Simulates auction bid processing:
 * - Generates synthetic bid events
 * - Processes bids by auction
 * - Configurable event rate to test scheduler adaptation
 */
public class NexmarkBenchmarkJob {
    
    public static void main(String[] args) throws Exception {
        
        // Parse arguments
        int eventsPerSecond = 10000;  // Default: 10k events/sec
        int durationSeconds = 300;     // Default: 5 minutes
        
        if (args.length > 0) {
            eventsPerSecond = Integer.parseInt(args[0]);
        }
        if (args.length > 1) {
            durationSeconds = Integer.parseInt(args[1]);
        }
        
        System.out.println("==========================================");
        System.out.println("  Nexmark Benchmark - Bid Processing");
        System.out.println("==========================================");
        System.out.println("Events per second: " + eventsPerSecond);
        System.out.println("Duration: " + durationSeconds + " seconds");
        System.out.println("Total events: " + (eventsPerSecond * durationSeconds));
        System.out.println("==========================================\n");
        
        // Setup Flink environment
        StreamExecutionEnvironment env = StreamExecutionEnvironment.getExecutionEnvironment();
        env.setParallelism(4); // Match TaskManager slots
        
        // Generate bid events
        DataStream<Bid> bids = env.addSource(
            new BidSourceFunction(eventsPerSecond, durationSeconds)
        ).name("Bid Source");
        
        // Process: Convert USD to EUR and aggregate by auction
        bids
            .flatMap(new ConvertToEuros())
            .name("Convert to EUR")
            .keyBy(tuple -> tuple.f0)  // Key by auction ID
            .sum(1)                     // Sum prices
            .print()
            .name("Aggregated Bids in EUR");
        
        // Execute
        env.execute("Nexmark Benchmark - Adaptive Scheduler Test");
    }
    
    /**
     * Bid event
     */
    public static class Bid {
        public long auctionId;
        public long bidderId;
        public double price;      // In USD
        public long timestamp;
        
        public Bid(long auctionId, long bidderId, double price, long timestamp) {
            this.auctionId = auctionId;
            this.bidderId = bidderId;
            this.price = price;
            this.timestamp = timestamp;
        }
        
        @Override
        public String toString() {
            return String.format("Bid{auction=%d, bidder=%d, price=$%.2f}", 
                auctionId, bidderId, price);
        }
    }
    
    /**
     * Generate synthetic bid events
     */
    public static class BidSourceFunction implements SourceFunction<Bid> {
        
        private final int eventsPerSecond;
        private final int durationSeconds;
        private volatile boolean running = true;
        
        public BidSourceFunction(int eventsPerSecond, int durationSeconds) {
            this.eventsPerSecond = eventsPerSecond;
            this.durationSeconds = durationSeconds;
        }
        
        @Override
        public void run(SourceContext<Bid> ctx) throws Exception {
            Random random = new Random();
            long startTime = System.currentTimeMillis();
            long endTime = startTime + (durationSeconds * 1000L);
            
            long eventCount = 0;
            long lastLogTime = startTime;
            
            while (running && System.currentTimeMillis() < endTime) {
                long currentTime = System.currentTimeMillis();
                
                // Generate batch of events
                for (int i = 0; i < eventsPerSecond / 10; i++) {
                    long auctionId = random.nextInt(1000);      // 1000 auctions
                    long bidderId = random.nextInt(10000);      // 10k bidders
                    double price = 10 + (random.nextDouble() * 990);  // $10-$1000
                    
                    ctx.collect(new Bid(auctionId, bidderId, price, currentTime));
                    eventCount++;
                }
                
                // Log progress every 10 seconds
                if (currentTime - lastLogTime > 10000) {
                    double rate = eventCount / ((currentTime - startTime) / 1000.0);
                    System.out.println(String.format(
                        "[Source] Generated %,d events (%.0f events/sec)", 
                        eventCount, rate
                    ));
                    lastLogTime = currentTime;
                }
                
                // Sleep to maintain rate
                Thread.sleep(100);
            }
            
            System.out.println(String.format(
                "[Source] Completed: %,d total events generated", eventCount
            ));
        }
        
        @Override
        public void cancel() {
            running = false;
        }
    }
    
    /**
     * Convert bid prices from USD to EUR
     */
    public static class ConvertToEuros implements FlatMapFunction<Bid, Tuple2<Long, Double>> {
        
        private static final double USD_TO_EUR = 0.908;
        
        @Override
        public void flatMap(Bid bid, Collector<Tuple2<Long, Double>> out) {
            double priceInEur = bid.price * USD_TO_EUR;
            out.collect(new Tuple2<>(bid.auctionId, priceInEur));
        }
    }
}