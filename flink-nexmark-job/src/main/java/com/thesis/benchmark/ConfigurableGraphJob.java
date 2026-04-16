package com.thesis.benchmark;

import org.apache.flink.api.common.eventtime.WatermarkStrategy;
import org.apache.flink.api.common.functions.AggregateFunction;
import org.apache.flink.api.common.functions.MapFunction;
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
import java.time.Duration;
import java.util.Arrays;
import java.util.Random;
import java.util.concurrent.atomic.AtomicLong;

/**
 * Configurable Graph Job para benchmark Nexmark.
 *
 * CARACTERÍSTICAS:
 * 1. Source no-bloqueante con cola interna y drop policy
 * 2. Pre-pool de eventos para minimizar overhead de generación
 * 3. Operador CPU-load configurable
 * 4. Distribuciones de llegada: CONSTANT, STEP, SINE
 * 5. Tracking de latencia end-to-end (procesamiento y total con ventana)
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

        // Latencia de procesamiento: event-time -> fin de CPU-load
        DataStream<Bid> trackedProcessing = loaded.map(new ProcessingLatencyTracker())
            .name("Latency Tracker (processing)")
            .setParallelism(config.globalParallelism);

        DataStream<Tuple2<Long, Double>> transformed = applyTransformations(trackedProcessing, config);
        DataStream<Tuple2<Long, Double>> windowed = applyWindows(transformed, config);
        applyTrackedSinks(windowed, config);

        env.execute(config.getJobName());
    }

    private static DataStream<Bid> buildSource(StreamExecutionEnvironment env, GraphConfig config) {
        return env.addSource(new BidSource(config))
            .setParallelism(config.sourceParallelism)
            .name("Source: Bid Generator")
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

    private static DataStream<Bid> applyCpuLoad(DataStream<Bid> stream, GraphConfig config) {
        if (config.cpuLoadIterationsPerEvent <= 0) {
            return stream;
        }
        final int iterations = config.cpuLoadIterationsPerEvent;
        return stream.map(bid -> {
            double sink = bid.price;
            for (int i = 1; i <= iterations; i++) {
                sink = Math.sqrt(sink * i + 1.0) + Math.log1p(sink);
            }
            bid.bidderId ^= Double.doubleToLongBits(sink);
            return bid;
        }).name("CPU Load Simulator (" + iterations + " iter/event)")
          .setParallelism(config.globalParallelism);
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

    private static AggregateFunction<Tuple2<Long, Double>, ?, Tuple2<Long, Double>>
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

    private static void applyTrackedSinks(DataStream<Tuple2<Long, Double>> stream, GraphConfig config) {
        if (config.enableConsoleSink) {
            stream.addSink(new TotalLatencySink())
                .name("Sink: Tracked Console")
                .setParallelism(config.sinkParallelism);
        }
    }

    // ========================================
    // LATENCY TRACKING
    // ========================================

    /**
     * Mide latencia de procesamiento: event-time -> fin de CPU-load.
     * NO incluye el delay del window (esa es la métrica más util para comparar
     * estrategias porque mide cuánto demora el pipeline en procesar un evento).
     */
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
                        System.out.printf(
                            "[Latency-PROC-%d] count=%d min=%dms p50=%dms p95=%dms p99=%dms max=%dms avg=%.1fms%n",
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
            if (latency >= 0) {
                histogram.add(latency);
            }
            return bid;
        }

        @Override
        public void close() {
            running = false;
            LatencyStats s = histogram.snapshot();
            if (s.count > 0) {
                System.out.printf(
                    "[Latency-PROC-%d] FINAL count=%d min=%dms p50=%dms p95=%dms p99=%dms max=%dms avg=%.1fms%n",
                    subtaskIndex, s.count, s.min, s.p50, s.p95, s.p99, s.max, s.avg);
            }
        }
    }

    /**
     * Sink que mide latencia total (event-time del agregado -> sink).
     * Para tumbling windows, el event-time es el max timestamp de la ventana,
     * por lo que esta latencia incluye el delay del window.
     */
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
            this.firstRecordTime = 0;
            this.lastRecordTime = 0;
            this.subtaskIndex = getRuntimeContext().getIndexOfThisSubtask();
            this.running = true;

            this.loggerThread = new Thread(() -> {
                long lastCount = 0;
                long lastTime = System.currentTimeMillis();
                while (running) {
                    try { Thread.sleep(10000); } catch (InterruptedException e) { return; }
                    long now = System.currentTimeMillis();
                    long c = recordCount.get();
                    double rate = (c - lastCount) / ((now - lastTime) / 1000.0);
                    System.out.printf("[Sink-%d] records=%d rate=%.0f/s%n",
                        subtaskIndex, c, rate);
                    lastCount = c;
                    lastTime = now;
                }
            }, "sink-logger-" + subtaskIndex);
            loggerThread.setDaemon(true);
            loggerThread.start();
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
                if (latency >= 0) {
                    sinkHistogram.add(latency);
                }
            }
        }

        @Override
        public void close() {
            running = false;
            long c = recordCount.get();
            LatencyStats s = sinkHistogram.snapshot();
            System.out.printf("[Sink-%d] FINAL records=%d firstRecord=%d lastRecord=%d%n",
                subtaskIndex, c, firstRecordTime, lastRecordTime);
            if (s.count > 0) {
                System.out.printf(
                    "[Latency-TOTAL-%d] FINAL count=%d min=%dms p50=%dms p95=%dms p99=%dms max=%dms avg=%.1fms%n",
                    subtaskIndex, s.count, s.min, s.p50, s.p95, s.p99, s.max, s.avg);
            }
        }
    }

    /**
     * Histograma ligero con reservoir sampling para percentiles.
     */
    public static class LatencyHistogram {
        private static final int RESERVOIR_SIZE = 10_000;
        private final long[] reservoir = new long[RESERVOIR_SIZE];
        private long count = 0;
        private long sum = 0;
        private long min = Long.MAX_VALUE;
        private long max = 0;
        private final Random random = new Random();

        public synchronized void add(long latencyMs) {
            count++;
            sum += latencyMs;
            if (latencyMs < min) min = latencyMs;
            if (latencyMs > max) max = latencyMs;

            if (count <= RESERVOIR_SIZE) {
                reservoir[(int)(count - 1)] = latencyMs;
            } else {
                long idx = random.nextLong() & Long.MAX_VALUE;
                idx = idx % count;
                if (idx < RESERVOIR_SIZE) {
                    reservoir[(int) idx] = latencyMs;
                }
            }
        }

        public synchronized LatencyStats snapshot() {
            if (count == 0) {
                return new LatencyStats(0, 0, 0, 0, 0, 0, 0, 0.0);
            }
            int n = (int) Math.min(count, RESERVOIR_SIZE);
            long[] sorted = Arrays.copyOf(reservoir, n);
            Arrays.sort(sorted);
            long p50 = sorted[Math.min(n - 1, (int)(n * 0.50))];
            long p95 = sorted[Math.min(n - 1, (int)(n * 0.95))];
            long p99 = sorted[Math.min(n - 1, (int)(n * 0.99))];
            return new LatencyStats(count, min, p50, p95, p99, max, sum, (double) sum / count);
        }
    }

    public static class LatencyStats {
        public final long count, min, p50, p95, p99, max, sum;
        public final double avg;
        public LatencyStats(long count, long min, long p50, long p95, long p99, long max, long sum, double avg) {
            this.count = count; this.min = min; this.p50 = p50; this.p95 = p95;
            this.p99 = p99; this.max = max; this.sum = sum; this.avg = avg;
        }
    }

    // ========================================
    // DATA CLASSES
    // ========================================

    public static class Bid {
        public long auctionId;
        public long bidderId;
        public double price;
        public long timestamp;

        public Bid() {}

        public Bid(long auctionId, long bidderId, double price, long timestamp) {
            this.auctionId = auctionId;
            this.bidderId = bidderId;
            this.price = price;
            this.timestamp = timestamp;
        }

        @Override
        public String toString() {
            return String.format("Bid(auction=%d, bidder=%d, price=%.2f)",
                auctionId, bidderId, price);
        }
    }

    // ========================================
    // SOURCE - NON-BLOCKING WITH VARIABLE RATE
    // ========================================

    public static class BidSource extends RichParallelSourceFunction<Bid> {
        private static final long serialVersionUID = 1L;

        private final GraphConfig config;
        private volatile boolean running = true;

        private static final int QUEUE_CAPACITY_FACTOR = 2;

        public BidSource(GraphConfig config) {
            this.config = config;
        }

        @Override
        public void run(SourceContext<Bid> ctx) throws Exception {
            int parallelism   = getRuntimeContext().getNumberOfParallelSubtasks();
            int subtaskIndex  = getRuntimeContext().getIndexOfThisSubtask();

            int peakRate;
            if (config.arrivalDistribution == GraphConfig.ArrivalDistribution.STEP) {
                peakRate = (int)(config.eventsPerSecond * config.stepHighRateFraction);
            } else if (config.arrivalDistribution == GraphConfig.ArrivalDistribution.SINE) {
                peakRate = (int)(config.eventsPerSecond * (1.0 + config.sineAmplitude));
            } else {
                peakRate = config.eventsPerSecond;
            }
            int maxRatePerInstance = Math.max(1, peakRate / parallelism);
            int queueCapacity = maxRatePerInstance * QUEUE_CAPACITY_FACTOR;

            java.util.concurrent.ArrayBlockingQueue<Bid> queue =
                new java.util.concurrent.ArrayBlockingQueue<>(queueCapacity);

            java.util.concurrent.atomic.AtomicLong generated = new java.util.concurrent.atomic.AtomicLong(0);
            java.util.concurrent.atomic.AtomicLong dropped   = new java.util.concurrent.atomic.AtomicLong(0);
            java.util.concurrent.atomic.AtomicLong emitted   = new java.util.concurrent.atomic.AtomicLong(0);

            long start = System.currentTimeMillis();
            long end   = start + (config.durationSeconds * 1000L);

            System.out.printf("[Source-%d/%d] NON-BLOCKING source: dist=%s, baseRate=%d ev/s, "
                + "peakRate=%d ev/s, queueCap=%d%n",
                subtaskIndex + 1, parallelism, config.arrivalDistribution,
                config.eventsPerSecond, peakRate, queueCapacity);

            Thread producer = new Thread(() -> {
                Random random = new Random();
                int poolSize = 10_000;
                Bid[] pool = new Bid[poolSize];
                for (int i = 0; i < poolSize; i++) {
                    pool[i] = new Bid(
                        random.nextInt(1000),
                        random.nextInt(10000),
                        10 + random.nextDouble() * 990,
                        0L
                    );
                }

                int batchesPerSec = 50;
                long batchIntervalNanos = 1_000_000_000L / batchesPerSec;
                long nextBatchTime = System.nanoTime();
                int poolIdx = 0;

                int currentRatePerInstance = Math.max(1, config.eventsPerSecond / parallelism);
                int currentBatchSize = Math.max(1, currentRatePerInstance / batchesPerSec);
                long lastRateUpdateMs = System.currentTimeMillis();

                while (running && System.currentTimeMillis() < end) {
                    long now = System.currentTimeMillis();

                    if (now - lastRateUpdateMs >= 1000) {
                        double elapsedSec = (now - start) / 1000.0;
                        int globalRate = config.getInstantRate(elapsedSec);
                        currentRatePerInstance = Math.max(1, globalRate / parallelism);
                        currentBatchSize = Math.max(1, currentRatePerInstance / batchesPerSec);
                        lastRateUpdateMs = now;
                    }

                    for (int i = 0; i < currentBatchSize; i++) {
                        Bid template = pool[poolIdx];
                        poolIdx = (poolIdx + 1) % poolSize;
                        Bid bid = new Bid(template.auctionId, template.bidderId,
                                          template.price, now);
                        generated.incrementAndGet();
                        if (!queue.offer(bid)) {
                            dropped.incrementAndGet();
                        }
                    }

                    nextBatchTime += batchIntervalNanos;
                    long sleepNanos = nextBatchTime - System.nanoTime();
                    if (sleepNanos > 0) {
                        try {
                            Thread.sleep(sleepNanos / 1_000_000, (int)(sleepNanos % 1_000_000));
                        } catch (InterruptedException e) {
                            Thread.currentThread().interrupt();
                            return;
                        }
                    } else {
                        nextBatchTime = System.nanoTime();
                    }
                }
            }, "bid-producer-" + subtaskIndex);
            producer.setDaemon(true);
            producer.start();

            Thread logger = new Thread(() -> {
                long lastGen = 0, lastDrop = 0, lastEmit = 0;
                long lastTime = System.currentTimeMillis();
                while (running && System.currentTimeMillis() < end) {
                    try { Thread.sleep(10000); } catch (InterruptedException e) { return; }
                    long now = System.currentTimeMillis();
                    long g = generated.get(), d = dropped.get(), e = emitted.get();
                    double dtSec = (now - lastTime) / 1000.0;
                    double genRate  = (g - lastGen)  / dtSec;
                    double dropRate = (d - lastDrop) / dtSec;
                    double emitRate = (e - lastEmit) / dtSec;
                    double dropPct  = g > 0 ? (d * 100.0 / g) : 0.0;
                    double elapsed  = (now - start) / 1000.0;
                    int targetRate  = config.getInstantRate(elapsed);
                    System.out.printf(
                        "[Source-%d] t=%.0fs target=%dk/s gen=%,d (%.0f/s) emit=%,d (%.0f/s) "
                        + "drop=%,d (%.0f/s, %.1f%%) qDepth=%d%n",
                        subtaskIndex + 1, elapsed, targetRate / 1000,
                        g, genRate, e, emitRate, d, dropRate, dropPct, queue.size());
                    lastGen = g; lastDrop = d; lastEmit = e; lastTime = now;
                }
            }, "bid-logger-" + subtaskIndex);
            logger.setDaemon(true);
            logger.start();

            while (running && (System.currentTimeMillis() < end || !queue.isEmpty())) {
                Bid bid = queue.poll(100, java.util.concurrent.TimeUnit.MILLISECONDS);
                if (bid == null) continue;
                synchronized (ctx.getCheckpointLock()) {
                    ctx.collect(bid);
                }
                emitted.incrementAndGet();
            }

            long g = generated.get(), d = dropped.get(), e = emitted.get();
            double dropPct = g > 0 ? (d * 100.0 / g) : 0.0;
            long durationMs = System.currentTimeMillis() - start;
            System.out.printf(
                "[Source-%d] FINALIZADO en %.1fs: generated=%,d, emitted=%,d, dropped=%,d (%.1f%%)%n",
                subtaskIndex + 1, durationMs / 1000.0, g, e, d, dropPct);
        }

        @Override
        public void cancel() {
            running = false;
        }
    }

    // ========================================
    // TRANSFORMERS
    // ========================================

    public static class CurrencyConverter implements MapFunction<Bid, Tuple2<Long, Double>> {
        private static final long serialVersionUID = 1L;
        private static final double EUR_RATE = 0.908;

        @Override
        public Tuple2<Long, Double> map(Bid bid) {
            return new Tuple2<>(bid.auctionId, bid.price * EUR_RATE);
        }
    }

    // ========================================
    // AGGREGATORS
    // ========================================

    public static class SumAggregator implements
            AggregateFunction<Tuple2<Long, Double>, Tuple2<Long, Double>, Tuple2<Long, Double>> {
        private static final long serialVersionUID = 1L;
        @Override public Tuple2<Long, Double> createAccumulator() { return new Tuple2<>(0L, 0.0); }
        @Override public Tuple2<Long, Double> add(Tuple2<Long, Double> v, Tuple2<Long, Double> a) {
            return new Tuple2<>(v.f0, a.f1 + v.f1);
        }
        @Override public Tuple2<Long, Double> getResult(Tuple2<Long, Double> a) { return a; }
        @Override public Tuple2<Long, Double> merge(Tuple2<Long, Double> a, Tuple2<Long, Double> b) {
            return new Tuple2<>(a.f0, a.f1 + b.f1);
        }
    }

    public static class AverageAggregator implements
            AggregateFunction<Tuple2<Long, Double>, Tuple3<Long, Double, Long>, Tuple2<Long, Double>> {
        private static final long serialVersionUID = 1L;
        @Override public Tuple3<Long, Double, Long> createAccumulator() { return new Tuple3<>(0L, 0.0, 0L); }
        @Override public Tuple3<Long, Double, Long> add(Tuple2<Long, Double> v, Tuple3<Long, Double, Long> a) {
            return new Tuple3<>(v.f0, a.f1 + v.f1, a.f2 + 1);
        }
        @Override public Tuple2<Long, Double> getResult(Tuple3<Long, Double, Long> a) {
            return new Tuple2<>(a.f0, a.f2 > 0 ? a.f1 / a.f2 : 0.0);
        }
        @Override public Tuple3<Long, Double, Long> merge(Tuple3<Long, Double, Long> a, Tuple3<Long, Double, Long> b) {
            return new Tuple3<>(a.f0, a.f1 + b.f1, a.f2 + b.f2);
        }
    }

    public static class CountAggregator implements
            AggregateFunction<Tuple2<Long, Double>, Tuple2<Long, Long>, Tuple2<Long, Double>> {
        private static final long serialVersionUID = 1L;
        @Override public Tuple2<Long, Long> createAccumulator() { return new Tuple2<>(0L, 0L); }
        @Override public Tuple2<Long, Long> add(Tuple2<Long, Double> v, Tuple2<Long, Long> a) {
            return new Tuple2<>(v.f0, a.f1 + 1);
        }
        @Override public Tuple2<Long, Double> getResult(Tuple2<Long, Long> a) {
            return new Tuple2<>(a.f0, (double) a.f1);
        }
        @Override public Tuple2<Long, Long> merge(Tuple2<Long, Long> a, Tuple2<Long, Long> b) {
            return new Tuple2<>(a.f0, a.f1 + b.f1);
        }
    }

    public static class MaxAggregator implements
            AggregateFunction<Tuple2<Long, Double>, Tuple2<Long, Double>, Tuple2<Long, Double>> {
        private static final long serialVersionUID = 1L;
        @Override public Tuple2<Long, Double> createAccumulator() { return new Tuple2<>(0L, Double.NEGATIVE_INFINITY); }
        @Override public Tuple2<Long, Double> add(Tuple2<Long, Double> v, Tuple2<Long, Double> a) {
            return new Tuple2<>(v.f0, Math.max(a.f1, v.f1));
        }
        @Override public Tuple2<Long, Double> getResult(Tuple2<Long, Double> a) { return a; }
        @Override public Tuple2<Long, Double> merge(Tuple2<Long, Double> a, Tuple2<Long, Double> b) {
            return new Tuple2<>(a.f0, Math.max(a.f1, b.f1));
        }
    }

    public static class MinAggregator implements
            AggregateFunction<Tuple2<Long, Double>, Tuple2<Long, Double>, Tuple2<Long, Double>> {
        private static final long serialVersionUID = 1L;
        @Override public Tuple2<Long, Double> createAccumulator() { return new Tuple2<>(0L, Double.POSITIVE_INFINITY); }
        @Override public Tuple2<Long, Double> add(Tuple2<Long, Double> v, Tuple2<Long, Double> a) {
            return new Tuple2<>(v.f0, Math.min(a.f1, v.f1));
        }
        @Override public Tuple2<Long, Double> getResult(Tuple2<Long, Double> a) { return a; }
        @Override public Tuple2<Long, Double> merge(Tuple2<Long, Double> a, Tuple2<Long, Double> b) {
            return new Tuple2<>(a.f0, Math.min(a.f1, b.f1));
        }
    }
}