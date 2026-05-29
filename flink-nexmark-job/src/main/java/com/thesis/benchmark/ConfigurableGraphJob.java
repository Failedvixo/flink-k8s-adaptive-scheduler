package com.thesis.benchmark;

import org.apache.flink.api.common.eventtime.WatermarkStrategy;
import org.apache.flink.api.common.functions.AggregateFunction;
import org.apache.flink.api.common.functions.MapFunction;
import org.apache.flink.api.common.functions.RichFlatMapFunction;
import org.apache.flink.api.java.tuple.Tuple2;
import org.apache.flink.api.java.tuple.Tuple3;
import org.apache.flink.streaming.api.datastream.DataStream;
import org.apache.flink.streaming.api.environment.StreamExecutionEnvironment;
import org.apache.flink.streaming.api.functions.source.RichParallelSourceFunction;
import org.apache.flink.streaming.api.functions.sink.RichSinkFunction;
import org.apache.flink.streaming.api.windowing.assigners.EventTimeSessionWindows;
import org.apache.flink.streaming.api.windowing.assigners.SlidingEventTimeWindows;
import org.apache.flink.streaming.api.windowing.assigners.TumblingEventTimeWindows;
import org.apache.flink.streaming.api.windowing.time.Time;
import org.apache.flink.configuration.Configuration;
import org.apache.flink.util.Collector;
import java.time.Duration;
import java.util.Arrays;
import java.util.Random;
import java.util.concurrent.atomic.AtomicLong;

/**
 * Configurable Graph Job for Nexmark benchmark.
 *
 * Key features for auto-scaling:
 * - CPU-load has .disableChaining() → own vertex with independent busy% metric
 * - CPU-load has cpuLoadParallelism independent from globalParallelism
 * - All operators have stable .uid() for savepoint/restore compatibility
 * - Flink Adaptive Scheduler can rescale CPU-load in-place via REST API
 */
public class ConfigurableGraphJob {

    public static void main(String[] args) throws Exception {
        GraphConfig config = GraphConfig.fromArgs(args);
        config.print();

        StreamExecutionEnvironment env = StreamExecutionEnvironment.getExecutionEnvironment();
        env.setParallelism(config.globalParallelism);

        DataStream<Bid> bids = buildSource(env, config);
        DataStream<Bid> filtered = applyFilters(bids, config);
        DataStream<Bid> loaded = applyCpuLoad(filtered, config);

        DataStream<Bid> trackedProcessing = loaded.map(new ProcessingLatencyTracker())
            .uid("latency-tracker")
            .name("Latency Tracker (processing)")
            .setParallelism(config.globalParallelism);

        DataStream<Tuple2<Long, Double>> transformed = applyTransformations(trackedProcessing, config);
        DataStream<Tuple2<Long, Double>> windowed = applyWindows(transformed, config);
        applyTrackedSinks(windowed, config);

        env.execute(config.getJobName());
    }

    private static DataStream<Bid> buildSource(StreamExecutionEnvironment env, GraphConfig config) {
        return env.addSource(new BidSource(config))
            .uid("bid-source")
            .setParallelism(config.sourceParallelism)
            .name("Source: Bid Generator")
            .assignTimestampsAndWatermarks(
                WatermarkStrategy.<Bid>forBoundedOutOfOrderness(Duration.ofSeconds(5))
                    .withTimestampAssigner((bid, ts) -> bid.timestamp))
            .uid("watermark-assigner")
            .name("Watermark Assigner");
    }

    private static DataStream<Bid> applyFilters(DataStream<Bid> stream, GraphConfig config) {
        if (config.enableHighValueFilter) {
            stream = stream.filter(bid -> bid.price > config.minBidPrice)
                .uid("filter-high-value")
                .name("Filter: High Value Bids");
        }
        if (config.enableAuctionFilter) {
            stream = stream.filter(bid -> bid.auctionId < config.maxAuctionId)
                .uid("filter-auction")
                .name("Filter: Auction Range");
        }
        if (config.enableBidderFilter) {
            stream = stream.filter(bid -> bid.bidderId < config.maxBidderId)
                .uid("filter-bidder")
                .name("Filter: Bidder Range");
        }
        return stream;
    }

    /**
     * CPU-load with:
     * - .disableChaining() → separate vertex with its own busy% metric
     * - .setParallelism(cpuLoadParallelism) → independent from global
     * - .uid("cpu-load-simulator") → stable ID for rescaling
     * - Staleness policy: drops events older than maxEventAgeMs
     */
    private static DataStream<Bid> applyCpuLoad(DataStream<Bid> stream, GraphConfig config) {
        if (config.cpuLoadIterationsPerEvent <= 0 && config.maxEventAgeMs <= 0) {
            return stream;
        }

        final int iterations = config.cpuLoadIterationsPerEvent;
        final long maxAge = config.maxEventAgeMs;

        return stream.flatMap(new RichFlatMapFunction<Bid, Bid>() {
            private static final long serialVersionUID = 1L;

            private transient long processed;
            private transient long staleDropped;
            private transient int subtaskIndex;
            private transient int parallelism;
            private transient Thread loggerThread;
            private transient volatile boolean running;
            private transient long startTime;

            @Override
            public void open(Configuration parameters) {
                this.processed = 0;
                this.staleDropped = 0;
                this.subtaskIndex = getRuntimeContext().getIndexOfThisSubtask();
                this.parallelism = getRuntimeContext().getNumberOfParallelSubtasks();
                this.startTime = System.currentTimeMillis();
                this.running = true;

                System.out.printf("[CPULoad-%d/%d] Starting: iterations=%d maxAge=%dms%n",
                    subtaskIndex, parallelism, iterations, maxAge);

                this.loggerThread = new Thread(() -> {
                    long lastProcessed = 0;
                    long lastStale = 0;
                    long lastTime = System.currentTimeMillis();
                    while (running) {
                        try { Thread.sleep(10000); } catch (InterruptedException e) { return; }
                        long now = System.currentTimeMillis();
                        long p = processed;
                        long s = staleDropped;
                        double dtSec = (now - lastTime) / 1000.0;
                        double procRate = (p - lastProcessed) / dtSec;
                        double staleRate = (s - lastStale) / dtSec;
                        double stalePct = (p + s) > 0 ? (s * 100.0 / (p + s)) : 0.0;
                        double elapsed = (now - startTime) / 1000.0;
                        System.out.printf(
                            "[CPULoad-%d] t=%.0fs processed=%,d (%.0f/s) staleDropped=%,d (%.0f/s, %.1f%%)%n",
                            subtaskIndex, elapsed, p, procRate, s, staleRate, stalePct);
                        lastProcessed = p;
                        lastStale = s;
                        lastTime = now;
                    }
                }, "cpuload-logger-" + subtaskIndex);
                loggerThread.setDaemon(true);
                loggerThread.start();
            }

            @Override
            public void flatMap(Bid bid, Collector<Bid> out) {
                if (maxAge > 0) {
                    long age = System.currentTimeMillis() - bid.timestamp;
                    if (age > maxAge) {
                        staleDropped++;
                        return;
                    }
                }
                if (iterations > 0) {
                    double sink = bid.price;
                    for (int i = 1; i <= iterations; i++) {
                        sink = Math.sqrt(sink * i + 1.0) + Math.log1p(sink);
                    }
                    bid.bidderId ^= Double.doubleToLongBits(sink);
                }
                processed++;
                out.collect(bid);
            }

            @Override
            public void close() {
                running = false;
                long total = processed + staleDropped;
                double stalePct = total > 0 ? (staleDropped * 100.0 / total) : 0.0;
                double durationSec = (System.currentTimeMillis() - startTime) / 1000.0;
                System.out.printf(
                    "[CPULoad-%d] FINAL duration=%.1fs processed=%,d staleDropped=%,d (%.1f%%) total=%,d%n",
                    subtaskIndex, durationSec, processed, staleDropped, stalePct, total);
            }
        })
          .uid("cpu-load-simulator")
          .name("CPU Load Simulator (" + iterations + " iter/event, maxAge=" + maxAge + "ms)")
          .setParallelism(config.cpuLoadParallelism)
          .disableChaining();
    }

    private static DataStream<Tuple2<Long, Double>> applyTransformations(
            DataStream<Bid> stream, GraphConfig config) {
        if (config.enableCurrencyConversion) {
            return stream.map(new CurrencyConverter())
                .uid("transform-currency")
                .name("Transform: USD to EUR")
                .setParallelism(config.transformParallelism);
        } else {
            return stream.map(bid -> new Tuple2<>(bid.auctionId, bid.price))
                .uid("transform-extract")
                .name("Transform: Extract Price")
                .setParallelism(config.transformParallelism);
        }
    }

    private static DataStream<Tuple2<Long, Double>> applyWindows(
            DataStream<Tuple2<Long, Double>> stream, GraphConfig config) {
        switch (config.windowType) {
            case TUMBLING:
                return stream.keyBy(t -> t.f0)
                    .window(TumblingEventTimeWindows.of(Time.seconds(config.windowSizeSeconds)))
                    .aggregate(createAggregator(config))
                    .uid("window-tumbling").name("Window: Tumbling");
            case SLIDING:
                return stream.keyBy(t -> t.f0)
                    .window(SlidingEventTimeWindows.of(Time.seconds(config.windowSizeSeconds), Time.seconds(config.slideSizeSeconds)))
                    .aggregate(createAggregator(config))
                    .uid("window-sliding").name("Window: Sliding");
            case SESSION:
                return stream.keyBy(t -> t.f0)
                    .window(EventTimeSessionWindows.withGap(Time.seconds(config.sessionGapSeconds)))
                    .aggregate(createAggregator(config))
                    .uid("window-session").name("Window: Session");
            default:
                throw new IllegalArgumentException("Unknown window type");
        }
    }

    private static AggregateFunction<Tuple2<Long, Double>, ?, Tuple2<Long, Double>> createAggregator(GraphConfig config) {
        switch (config.aggregationType) {
            case SUM: return new SumAggregator();
            case AVERAGE: return new AverageAggregator();
            case COUNT: return new CountAggregator();
            case MAX: return new MaxAggregator();
            case MIN: return new MinAggregator();
            default: throw new IllegalArgumentException("Unknown aggregation");
        }
    }

    private static void applyTrackedSinks(DataStream<Tuple2<Long, Double>> stream, GraphConfig config) {
        if (config.enableConsoleSink) {
            stream.addSink(new TotalLatencySink())
                .uid("sink-tracked").name("Sink: Tracked Console")
                .setParallelism(config.sinkParallelism);
        }
    }

    // ========== LATENCY TRACKING ==========

    public static class ProcessingLatencyTracker extends org.apache.flink.api.common.functions.RichMapFunction<Bid, Bid> {
        private static final long serialVersionUID = 1L;
        private transient LatencyHistogram histogram;
        private transient Thread loggerThread;
        private transient volatile boolean running;
        private transient int subtaskIndex;

        @Override
        public void open(Configuration parameters) {
            this.histogram = new LatencyHistogram();
            this.subtaskIndex = getRuntimeContext().getIndexOfThisSubtask();
            this.running = true;
            this.loggerThread = new Thread(() -> {
                while (running) {
                    try { Thread.sleep(10000); } catch (InterruptedException e) { return; }
                    LatencyStats s = histogram.snapshot();
                    if (s.count > 0) {
                        System.out.printf("[Latency-PROC-%d] count=%d min=%dms p50=%dms p95=%dms p99=%dms max=%dms avg=%.1fms%n",
                            subtaskIndex, s.count, s.min, s.p50, s.p95, s.p99, s.max, s.avg);
                    }
                }
            }, "latency-proc-logger-" + subtaskIndex);
            loggerThread.setDaemon(true);
            loggerThread.start();
        }

        @Override
        public Bid map(Bid bid) {
            long latency = System.currentTimeMillis() - bid.timestamp;
            if (latency >= 0) histogram.add(latency);
            return bid;
        }

        @Override
        public void close() {
            running = false;
            LatencyStats s = histogram.snapshot();
            if (s.count > 0) {
                System.out.printf("[Latency-PROC-%d] FINAL count=%d min=%dms p50=%dms p95=%dms p99=%dms max=%dms avg=%.1fms%n",
                    subtaskIndex, s.count, s.min, s.p50, s.p95, s.p99, s.max, s.avg);
            }
        }
    }

    public static class TotalLatencySink extends RichSinkFunction<Tuple2<Long, Double>> {
        private static final long serialVersionUID = 1L;
        private transient LatencyHistogram sinkHistogram;
        private transient AtomicLong recordCount;
        private transient long firstRecordTime;
        private transient long lastRecordTime;
        private transient Thread loggerThread;
        private transient volatile boolean running;
        private transient int subtaskIndex;

        @Override
        public void open(Configuration parameters) {
            this.sinkHistogram = new LatencyHistogram();
            this.recordCount = new AtomicLong(0);
            this.firstRecordTime = 0; this.lastRecordTime = 0;
            this.subtaskIndex = getRuntimeContext().getIndexOfThisSubtask();
            this.running = true;
            this.loggerThread = new Thread(() -> {
                long lastCount = 0; long lastTime = System.currentTimeMillis();
                while (running) {
                    try { Thread.sleep(10000); } catch (InterruptedException e) { return; }
                    long now = System.currentTimeMillis(); long c = recordCount.get();
                    double rate = (c - lastCount) / ((now - lastTime) / 1000.0);
                    System.out.printf("[Sink-%d] records=%d rate=%.0f/s%n", subtaskIndex, c, rate);
                    lastCount = c; lastTime = now;
                }
            }, "sink-logger-" + subtaskIndex);
            loggerThread.setDaemon(true); loggerThread.start();
        }

        @Override
        public void invoke(Tuple2<Long, Double> value, Context context) {
            long now = System.currentTimeMillis();
            long count = recordCount.incrementAndGet();
            if (count == 1) firstRecordTime = now;
            lastRecordTime = now;
            Long eventTs = context.timestamp();
            if (eventTs != null && eventTs > 0) {
                long latency = now - eventTs;
                if (latency >= 0) sinkHistogram.add(latency);
            }
        }

        @Override
        public void close() {
            running = false;
            long c = recordCount.get(); LatencyStats s = sinkHistogram.snapshot();
            System.out.printf("[Sink-%d] FINAL records=%d firstRecord=%d lastRecord=%d%n",
                subtaskIndex, c, firstRecordTime, lastRecordTime);
            if (s.count > 0) {
                System.out.printf("[Latency-TOTAL-%d] FINAL count=%d min=%dms p50=%dms p95=%dms p99=%dms max=%dms avg=%.1fms%n",
                    subtaskIndex, s.count, s.min, s.p50, s.p95, s.p99, s.max, s.avg);
            }
        }
    }

    public static class LatencyHistogram {
        private static final int RESERVOIR_SIZE = 10_000;
        private final long[] reservoir = new long[RESERVOIR_SIZE];
        private long count = 0; private long sum = 0;
        private long min = Long.MAX_VALUE; private long max = 0;
        private final Random random = new Random();

        public synchronized void add(long latencyMs) {
            count++; sum += latencyMs;
            if (latencyMs < min) min = latencyMs;
            if (latencyMs > max) max = latencyMs;
            if (count <= RESERVOIR_SIZE) { reservoir[(int)(count - 1)] = latencyMs; }
            else { long idx = (random.nextLong() & Long.MAX_VALUE) % count; if (idx < RESERVOIR_SIZE) reservoir[(int)idx] = latencyMs; }
        }

        public synchronized LatencyStats snapshot() {
            if (count == 0) return new LatencyStats(0,0,0,0,0,0,0,0.0);
            int n = (int) Math.min(count, RESERVOIR_SIZE);
            long[] sorted = Arrays.copyOf(reservoir, n); Arrays.sort(sorted);
            return new LatencyStats(count, min,
                sorted[Math.min(n-1,(int)(n*0.50))], sorted[Math.min(n-1,(int)(n*0.95))],
                sorted[Math.min(n-1,(int)(n*0.99))], max, sum, (double)sum/count);
        }
    }

    public static class LatencyStats {
        public final long count, min, p50, p95, p99, max, sum; public final double avg;
        public LatencyStats(long count, long min, long p50, long p95, long p99, long max, long sum, double avg) {
            this.count=count; this.min=min; this.p50=p50; this.p95=p95;
            this.p99=p99; this.max=max; this.sum=sum; this.avg=avg;
        }
    }

    // ========== DATA ==========

    public static class Bid {
        public long auctionId; public long bidderId; public double price; public long timestamp;
        public Bid() {}
        public Bid(long a, long b, double p, long t) { auctionId=a; bidderId=b; price=p; timestamp=t; }
        @Override public String toString() { return String.format("Bid(auction=%d,bidder=%d,price=%.2f)", auctionId, bidderId, price); }
    }

    // ========== SOURCE ==========

    public static class BidSource extends RichParallelSourceFunction<Bid> {
        private static final long serialVersionUID = 1L;
        private final GraphConfig config;
        private volatile boolean running = true;

        public BidSource(GraphConfig config) { this.config = config; }

        @Override
        public void run(SourceContext<Bid> ctx) throws Exception {
            int parallelism = getRuntimeContext().getNumberOfParallelSubtasks();
            int subtaskIndex = getRuntimeContext().getIndexOfThisSubtask();
            int peakRate;
            if (config.arrivalDistribution == GraphConfig.ArrivalDistribution.STEP)
                peakRate = (int)(config.eventsPerSecond * config.stepHighRateFraction);
            else if (config.arrivalDistribution == GraphConfig.ArrivalDistribution.SINE)
                peakRate = (int)(config.eventsPerSecond * (1.0 + config.sineAmplitude));
            else peakRate = config.eventsPerSecond;

            int maxRatePerInstance = Math.max(1, peakRate / parallelism);
            int queueCapacity = maxRatePerInstance * 2;
            java.util.concurrent.ArrayBlockingQueue<Bid> queue = new java.util.concurrent.ArrayBlockingQueue<>(queueCapacity);
            java.util.concurrent.atomic.AtomicLong generated = new java.util.concurrent.atomic.AtomicLong(0);
            java.util.concurrent.atomic.AtomicLong dropped = new java.util.concurrent.atomic.AtomicLong(0);
            java.util.concurrent.atomic.AtomicLong emitted = new java.util.concurrent.atomic.AtomicLong(0);
            long start = System.currentTimeMillis();
            long end = start + (config.durationSeconds * 1000L);

            System.out.printf("[Source-%d/%d] NON-BLOCKING: dist=%s baseRate=%d peakRate=%d qCap=%d maxAge=%dms%n",
                subtaskIndex+1, parallelism, config.arrivalDistribution, config.eventsPerSecond, peakRate, queueCapacity, config.maxEventAgeMs);

            Thread producer = new Thread(() -> {
                Random random = new Random();
                Bid[] pool = new Bid[10_000];
                for (int i = 0; i < pool.length; i++)
                    pool[i] = new Bid(random.nextInt(1000), random.nextInt(10000), 10+random.nextDouble()*990, 0L);
                int bps = 50; long batchNanos = 1_000_000_000L / bps;
                long nextBatch = System.nanoTime(); int poolIdx = 0;
                int ratePI = Math.max(1, config.eventsPerSecond / parallelism);
                int batchSize = Math.max(1, ratePI / bps);
                long lastRateUpdate = System.currentTimeMillis();

                while (running && System.currentTimeMillis() < end) {
                    long now = System.currentTimeMillis();
                    if (now - lastRateUpdate >= 1000) {
                        int globalRate = config.getInstantRate((now - start) / 1000.0);
                        ratePI = Math.max(1, globalRate / parallelism);
                        batchSize = Math.max(1, ratePI / bps);
                        lastRateUpdate = now;
                    }
                    for (int i = 0; i < batchSize; i++) {
                        Bid t = pool[poolIdx]; poolIdx = (poolIdx+1) % pool.length;
                        generated.incrementAndGet();
                        if (!queue.offer(new Bid(t.auctionId, t.bidderId, t.price, now))) dropped.incrementAndGet();
                    }
                    nextBatch += batchNanos;
                    long sleepNanos = nextBatch - System.nanoTime();
                    if (sleepNanos > 0) {
                        try { Thread.sleep(sleepNanos/1_000_000, (int)(sleepNanos%1_000_000)); }
                        catch (InterruptedException e) { return; }
                    } else nextBatch = System.nanoTime();
                }
            }, "bid-producer-" + subtaskIndex);
            producer.setDaemon(true); producer.start();

            Thread logger = new Thread(() -> {
                long lg=0, ld=0, le=0, lt=System.currentTimeMillis();
                while (running && System.currentTimeMillis() < end) {
                    try { Thread.sleep(10000); } catch (InterruptedException e) { return; }
                    long now=System.currentTimeMillis(); long g=generated.get(), d=dropped.get(), e=emitted.get();
                    double dt=(now-lt)/1000.0; double elapsed=(now-start)/1000.0;
                    System.out.printf("[Source-%d] t=%.0fs target=%dk/s gen=%,d (%.0f/s) emit=%,d (%.0f/s) drop=%,d (%.1f%%) q=%d%n",
                        subtaskIndex+1, elapsed, config.getInstantRate(elapsed)/1000,
                        g,(g-lg)/dt, e,(e-le)/dt, d, g>0?(d*100.0/g):0, queue.size());
                    lg=g; ld=d; le=e; lt=now;
                }
            }, "bid-logger-" + subtaskIndex);
            logger.setDaemon(true); logger.start();

            while (running && (System.currentTimeMillis() < end || !queue.isEmpty())) {
                Bid bid = queue.poll(100, java.util.concurrent.TimeUnit.MILLISECONDS);
                if (bid == null) continue;
                synchronized (ctx.getCheckpointLock()) { ctx.collect(bid); }
                emitted.incrementAndGet();
            }
            long g=generated.get(), d=dropped.get(), e=emitted.get();
            System.out.printf("[Source-%d] FINALIZADO en %.1fs: generated=%,d, emitted=%,d, dropped=%,d (%.1f%%)%n",
                subtaskIndex+1, (System.currentTimeMillis()-start)/1000.0, g, e, d, g>0?(d*100.0/g):0);
        }

        @Override public void cancel() { running = false; }
    }

    // ========== TRANSFORMERS & AGGREGATORS ==========

    public static class CurrencyConverter implements MapFunction<Bid, Tuple2<Long, Double>> {
        private static final long serialVersionUID = 1L;
        @Override public Tuple2<Long, Double> map(Bid bid) { return new Tuple2<>(bid.auctionId, bid.price * 0.908); }
    }

    public static class SumAggregator implements AggregateFunction<Tuple2<Long,Double>,Tuple2<Long,Double>,Tuple2<Long,Double>> {
        private static final long serialVersionUID = 1L;
        @Override public Tuple2<Long,Double> createAccumulator() { return new Tuple2<>(0L,0.0); }
        @Override public Tuple2<Long,Double> add(Tuple2<Long,Double> v, Tuple2<Long,Double> a) { return new Tuple2<>(v.f0, a.f1+v.f1); }
        @Override public Tuple2<Long,Double> getResult(Tuple2<Long,Double> a) { return a; }
        @Override public Tuple2<Long,Double> merge(Tuple2<Long,Double> a, Tuple2<Long,Double> b) { return new Tuple2<>(a.f0, a.f1+b.f1); }
    }
    public static class AverageAggregator implements AggregateFunction<Tuple2<Long,Double>,Tuple3<Long,Double,Long>,Tuple2<Long,Double>> {
        private static final long serialVersionUID = 1L;
        @Override public Tuple3<Long,Double,Long> createAccumulator() { return new Tuple3<>(0L,0.0,0L); }
        @Override public Tuple3<Long,Double,Long> add(Tuple2<Long,Double> v, Tuple3<Long,Double,Long> a) { return new Tuple3<>(v.f0, a.f1+v.f1, a.f2+1); }
        @Override public Tuple2<Long,Double> getResult(Tuple3<Long,Double,Long> a) { return new Tuple2<>(a.f0, a.f2>0?a.f1/a.f2:0.0); }
        @Override public Tuple3<Long,Double,Long> merge(Tuple3<Long,Double,Long> a, Tuple3<Long,Double,Long> b) { return new Tuple3<>(a.f0, a.f1+b.f1, a.f2+b.f2); }
    }
    public static class CountAggregator implements AggregateFunction<Tuple2<Long,Double>,Tuple2<Long,Long>,Tuple2<Long,Double>> {
        private static final long serialVersionUID = 1L;
        @Override public Tuple2<Long,Long> createAccumulator() { return new Tuple2<>(0L,0L); }
        @Override public Tuple2<Long,Long> add(Tuple2<Long,Double> v, Tuple2<Long,Long> a) { return new Tuple2<>(v.f0, a.f1+1); }
        @Override public Tuple2<Long,Double> getResult(Tuple2<Long,Long> a) { return new Tuple2<>(a.f0,(double)a.f1); }
        @Override public Tuple2<Long,Long> merge(Tuple2<Long,Long> a, Tuple2<Long,Long> b) { return new Tuple2<>(a.f0, a.f1+b.f1); }
    }
    public static class MaxAggregator implements AggregateFunction<Tuple2<Long,Double>,Tuple2<Long,Double>,Tuple2<Long,Double>> {
        private static final long serialVersionUID = 1L;
        @Override public Tuple2<Long,Double> createAccumulator() { return new Tuple2<>(0L,Double.NEGATIVE_INFINITY); }
        @Override public Tuple2<Long,Double> add(Tuple2<Long,Double> v, Tuple2<Long,Double> a) { return new Tuple2<>(v.f0, Math.max(a.f1,v.f1)); }
        @Override public Tuple2<Long,Double> getResult(Tuple2<Long,Double> a) { return a; }
        @Override public Tuple2<Long,Double> merge(Tuple2<Long,Double> a, Tuple2<Long,Double> b) { return new Tuple2<>(a.f0, Math.max(a.f1,b.f1)); }
    }
    public static class MinAggregator implements AggregateFunction<Tuple2<Long,Double>,Tuple2<Long,Double>,Tuple2<Long,Double>> {
        private static final long serialVersionUID = 1L;
        @Override public Tuple2<Long,Double> createAccumulator() { return new Tuple2<>(0L,Double.POSITIVE_INFINITY); }
        @Override public Tuple2<Long,Double> add(Tuple2<Long,Double> v, Tuple2<Long,Double> a) { return new Tuple2<>(v.f0, Math.min(a.f1,v.f1)); }
        @Override public Tuple2<Long,Double> getResult(Tuple2<Long,Double> a) { return a; }
        @Override public Tuple2<Long,Double> merge(Tuple2<Long,Double> a, Tuple2<Long,Double> b) { return new Tuple2<>(a.f0, Math.min(a.f1,b.f1)); }
    }
}