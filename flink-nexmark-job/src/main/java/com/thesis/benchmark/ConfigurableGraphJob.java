package com.thesis.benchmark;

import org.apache.flink.api.common.eventtime.WatermarkStrategy;
import org.apache.flink.api.common.functions.AggregateFunction;
import org.apache.flink.api.common.functions.MapFunction;
import org.apache.flink.api.java.tuple.Tuple2;
import org.apache.flink.streaming.api.datastream.DataStream;
import org.apache.flink.streaming.api.environment.StreamExecutionEnvironment;
import org.apache.flink.streaming.api.functions.source.SourceFunction;
import org.apache.flink.streaming.api.windowing.assigners.EventTimeSessionWindows;
import org.apache.flink.streaming.api.windowing.assigners.SlidingEventTimeWindows;
import org.apache.flink.streaming.api.windowing.assigners.TumblingEventTimeWindows;
import org.apache.flink.streaming.api.windowing.time.Time;
import java.time.Duration;
import java.util.Random;

public class ConfigurableGraphJob {
    
    public static void main(String[] args) throws Exception {
        GraphConfig config = GraphConfig.fromArgs(args);
        config.print();
        
        StreamExecutionEnvironment env = StreamExecutionEnvironment.getExecutionEnvironment();
        env.setParallelism(config.globalParallelism);
        
        DataStream<Bid> bids = buildSource(env, config);
        DataStream<Bid> filtered = applyFilters(bids, config);
        DataStream<Tuple2<Long, Double>> transformed = applyTransformations(filtered, config);
        DataStream<Tuple2<Long, Double>> windowed = applyWindows(transformed, config);
        applySinks(windowed, config);
        
        env.execute(config.getJobName());
    }
    
    private static DataStream<Bid> buildSource(StreamExecutionEnvironment env, GraphConfig config) {
        return env.addSource(new BidSource(config))
            .name("Source: Bid Generator")
            .setParallelism(config.sourceParallelism)
            .assignTimestampsAndWatermarks(
                WatermarkStrategy.<Bid>forBoundedOutOfOrderness(Duration.ofSeconds(5))
                    .withTimestampAssigner((bid, ts) -> bid.timestamp))
            .name("Watermark Assigner");
    }
    
    private static DataStream<Bid> applyFilters(DataStream<Bid> stream, GraphConfig config) {
        if (config.enableHighValueFilter) {
            stream = stream.filter(bid -> bid.price > config.minBidPrice)
                .name("Filter: High Value Bids");
        }
        if (config.enableAuctionFilter) {
            stream = stream.filter(bid -> bid.auctionId < config.maxAuctionId)
                .name("Filter: Auction Range");
        }
        if (config.enableBidderFilter) {
            stream = stream.filter(bid -> bid.bidderId < config.maxBidderId)
                .name("Filter: Bidder Range");
        }
        return stream;
    }
    
    private static DataStream<Tuple2<Long, Double>> applyTransformations(
            DataStream<Bid> stream, GraphConfig config) {
        if (config.enableCurrencyConversion) {
            return stream.map(new CurrencyConverter())
                .name("Transform: USD to EUR")
                .setParallelism(config.transformParallelism);
        } else {
            return stream.map(bid -> new Tuple2<>(bid.auctionId, bid.price))
                .name("Transform: Extract Price")
                .setParallelism(config.transformParallelism);
        }
    }
    
    private static DataStream<Tuple2<Long, Double>> applyWindows(
            DataStream<Tuple2<Long, Double>> stream, GraphConfig config) {
        
        switch (config.windowType) {
            case TUMBLING:
                return stream
                    .keyBy(tuple -> tuple.f0)
                    .window(TumblingEventTimeWindows.of(Time.seconds(config.windowSizeSeconds)))
                    .aggregate(createAggregator(config))
                    .name("Window: Tumbling");
            
            case SLIDING:
                return stream
                    .keyBy(tuple -> tuple.f0)
                    .window(SlidingEventTimeWindows.of(
                        Time.seconds(config.windowSizeSeconds),
                        Time.seconds(config.slideSizeSeconds)))
                    .aggregate(createAggregator(config))
                    .name("Window: Sliding");
            
            case SESSION:
                return stream
                    .keyBy(tuple -> tuple.f0)
                    .window(EventTimeSessionWindows.withGap(Time.seconds(config.sessionGapSeconds)))
                    .aggregate(createAggregator(config))
                    .name("Window: Session");
            
            default:
                throw new IllegalArgumentException("Unknown window type");
        }
    }
    
    private static AggregateFunction<Tuple2<Long, Double>, Tuple2<Long, Double>, Tuple2<Long, Double>> 
            createAggregator(GraphConfig config) {
        switch (config.aggregationType) {
            case SUM: return new SumAggregator();
            case AVERAGE: return new AverageAggregator();
            case COUNT: return new CountAggregator();
            case MAX: return new MaxAggregator();
            case MIN: return new MinAggregator();
            default: throw new IllegalArgumentException("Unknown aggregation");
        }
    }
    
    private static void applySinks(DataStream<Tuple2<Long, Double>> stream, GraphConfig config) {
        if (config.enableConsoleSink) {
            stream.print().name("Sink: Console").setParallelism(config.sinkParallelism);
        }
    }
    
    public static class Bid {
        public long auctionId;
        public long bidderId;
        public double price;
        public long timestamp;
        
        public Bid(long auctionId, long bidderId, double price, long timestamp) {
            this.auctionId = auctionId;
            this.bidderId = bidderId;
            this.price = price;
            this.timestamp = timestamp;
        }
    }
    
    public static class BidSource implements SourceFunction<Bid> {
        private final GraphConfig config;
        private volatile boolean running = true;
        
        public BidSource(GraphConfig config) {
            this.config = config;
        }
        
        public void run(SourceContext<Bid> ctx) throws Exception {
            Random random = new Random();
            long start = System.currentTimeMillis();
            long end = start + (config.durationSeconds * 1000L);
            long count = 0;
            long lastLog = start;
            
            while (running && System.currentTimeMillis() < end) {
                long now = System.currentTimeMillis();
                for (int i = 0; i < config.eventsPerSecond / 10; i++) {
                    ctx.collect(new Bid(random.nextInt(1000), random.nextInt(10000),
                        10 + random.nextDouble() * 990, now));
                    count++;
                }
                if (now - lastLog > 10000) {
                    double rate = count / ((now - start) / 1000.0);
                    System.out.println(String.format("[Source] Events: %,d | Rate: %.0f/s", count, rate));
                    lastLog = now;
                }
                Thread.sleep(100);
            }
        }
        
        public void cancel() {
            running = false;
        }
    }
    
    public static class CurrencyConverter implements MapFunction<Bid, Tuple2<Long, Double>> {
        public Tuple2<Long, Double> map(Bid bid) {
            return new Tuple2<>(bid.auctionId, bid.price * 0.908);
        }
    }
    
    public static class SumAggregator implements AggregateFunction<Tuple2<Long, Double>, Tuple2<Long, Double>, Tuple2<Long, Double>> {
        public Tuple2<Long, Double> createAccumulator() { return new Tuple2<>(0L, 0.0); }
        public Tuple2<Long, Double> add(Tuple2<Long, Double> value, Tuple2<Long, Double> acc) {
            return new Tuple2<>(value.f0, acc.f1 + value.f1);
        }
        public Tuple2<Long, Double> getResult(Tuple2<Long, Double> acc) { return acc; }
        public Tuple2<Long, Double> merge(Tuple2<Long, Double> a, Tuple2<Long, Double> b) {
            return new Tuple2<>(a.f0, a.f1 + b.f1);
        }
    }
    
    public static class AverageAggregator implements AggregateFunction<Tuple2<Long, Double>, Tuple2<Long, Double>, Tuple2<Long, Double>> {
        public Tuple2<Long, Double> createAccumulator() { return new Tuple2<>(0L, 0.0); }
        public Tuple2<Long, Double> add(Tuple2<Long, Double> value, Tuple2<Long, Double> acc) {
            return new Tuple2<>(acc.f0 + 1, acc.f1 + value.f1);
        }
        public Tuple2<Long, Double> getResult(Tuple2<Long, Double> acc) {
            return new Tuple2<>(acc.f0, acc.f0 > 0 ? acc.f1 / acc.f0 : 0.0);
        }
        public Tuple2<Long, Double> merge(Tuple2<Long, Double> a, Tuple2<Long, Double> b) {
            return new Tuple2<>(a.f0 + b.f0, a.f1 + b.f1);
        }
    }
    
    public static class CountAggregator implements AggregateFunction<Tuple2<Long, Double>, Tuple2<Long, Double>, Tuple2<Long, Double>> {
        public Tuple2<Long, Double> createAccumulator() { return new Tuple2<>(0L, 0.0); }
        public Tuple2<Long, Double> add(Tuple2<Long, Double> value, Tuple2<Long, Double> acc) {
            return new Tuple2<>(value.f0, acc.f1 + 1.0);
        }
        public Tuple2<Long, Double> getResult(Tuple2<Long, Double> acc) { return acc; }
        public Tuple2<Long, Double> merge(Tuple2<Long, Double> a, Tuple2<Long, Double> b) {
            return new Tuple2<>(a.f0, a.f1 + b.f1);
        }
    }
    
    public static class MaxAggregator implements AggregateFunction<Tuple2<Long, Double>, Tuple2<Long, Double>, Tuple2<Long, Double>> {
        public Tuple2<Long, Double> createAccumulator() { return new Tuple2<>(0L, Double.MIN_VALUE); }
        public Tuple2<Long, Double> add(Tuple2<Long, Double> value, Tuple2<Long, Double> acc) {
            return new Tuple2<>(value.f0, Math.max(acc.f1, value.f1));
        }
        public Tuple2<Long, Double> getResult(Tuple2<Long, Double> acc) { return acc; }
        public Tuple2<Long, Double> merge(Tuple2<Long, Double> a, Tuple2<Long, Double> b) {
            return new Tuple2<>(a.f0, Math.max(a.f1, b.f1));
        }
    }
    
    public static class MinAggregator implements AggregateFunction<Tuple2<Long, Double>, Tuple2<Long, Double>, Tuple2<Long, Double>> {
        public Tuple2<Long, Double> createAccumulator() { return new Tuple2<>(0L, Double.MAX_VALUE); }
        public Tuple2<Long, Double> add(Tuple2<Long, Double> value, Tuple2<Long, Double> acc) {
            return new Tuple2<>(value.f0, Math.min(acc.f1, value.f1));
        }
        public Tuple2<Long, Double> getResult(Tuple2<Long, Double> acc) { return acc; }
        public Tuple2<Long, Double> merge(Tuple2<Long, Double> a, Tuple2<Long, Double> b) {
            return new Tuple2<>(a.f0, Math.min(a.f1, b.f1));
        }
    }
}
