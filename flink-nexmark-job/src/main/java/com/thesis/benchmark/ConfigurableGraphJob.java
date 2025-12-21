package com.thesis.benchmark;

import org.apache.flink.api.common.eventtime.WatermarkStrategy;
import org.apache.flink.api.common.functions.AggregateFunction;
import org.apache.flink.api.common.functions.MapFunction;
import org.apache.flink.api.java.tuple.Tuple2;
import org.apache.flink.api.java.tuple.Tuple3;
import org.apache.flink.streaming.api.datastream.DataStream;
import org.apache.flink.streaming.api.environment.StreamExecutionEnvironment;
import org.apache.flink.streaming.api.functions.source.RichParallelSourceFunction;
import org.apache.flink.streaming.api.windowing.assigners.EventTimeSessionWindows;
import org.apache.flink.streaming.api.windowing.assigners.SlidingEventTimeWindows;
import org.apache.flink.streaming.api.windowing.assigners.TumblingEventTimeWindows;
import org.apache.flink.streaming.api.windowing.time.Time;
import java.time.Duration;
import java.util.Random;

/**
 * Configurable Graph Job para benchmark Nexmark
 * 
 * CORRECCIONES REALIZADAS:
 * 1. Rate limiting corregido con control preciso
 * 2. sourceParallelism ahora se aplica correctamente
 * 3. AverageAggregator corregido para mantener auctionId
 * 4. Uso de RichParallelSourceFunction para mejor escalabilidad
 * 5. Métricas de progreso mejoradas
 */
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
            .setParallelism(config.sourceParallelism)  // FIX: Ahora se aplica sourceParallelism
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
    
    private static void applySinks(DataStream<Tuple2<Long, Double>> stream, GraphConfig config) {
        if (config.enableConsoleSink) {
            stream.print().name("Sink: Console").setParallelism(config.sinkParallelism);
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
        
        public Bid() {} // Constructor vacío para serialización
        
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
    // SOURCE - CORREGIDO
    // ========================================
    
    /**
     * BidSource con rate limiting corregido.
     * Usa RichParallelSourceFunction para escalabilidad.
     */
    public static class BidSource extends RichParallelSourceFunction<Bid> {
        private static final long serialVersionUID = 1L;
        
        private final GraphConfig config;
        private volatile boolean running = true;
        
        public BidSource(GraphConfig config) {
            this.config = config;
        }
        
        @Override
        public void run(SourceContext<Bid> ctx) throws Exception {
            Random random = new Random();
            
            // Calcular tasa por instancia paralela
            int parallelism = getRuntimeContext().getNumberOfParallelSubtasks();
            int subtaskIndex = getRuntimeContext().getIndexOfThisSubtask();
            int eventsPerSecondPerInstance = config.eventsPerSecond / parallelism;
            
            // Configuración de batching
            int batchSize = Math.max(1, eventsPerSecondPerInstance / 100); // 100 batches/segundo
            long batchIntervalNanos = 10_000_000L; // 10ms entre batches
            
            long start = System.currentTimeMillis();
            long end = start + (config.durationSeconds * 1000L);
            long totalCount = 0;
            long lastLogTime = start;
            
            System.out.printf("[Source-%d/%d] Iniciando: %d evt/s por instancia, batch=%d%n",
                subtaskIndex + 1, parallelism, eventsPerSecondPerInstance, batchSize);
            
            while (running && System.currentTimeMillis() < end) {
                long batchStart = System.nanoTime();
                long now = System.currentTimeMillis();
                
                // Generar batch de eventos
                synchronized (ctx.getCheckpointLock()) {
                    for (int i = 0; i < batchSize; i++) {
                        Bid bid = new Bid(
                            random.nextInt(1000),      // auctionId: 0-999
                            random.nextInt(10000),     // bidderId: 0-9999
                            10 + random.nextDouble() * 990, // price: 10-1000
                            now
                        );
                        ctx.collect(bid);
                        totalCount++;
                    }
                }
                
                // Rate limiting preciso
                long elapsed = System.nanoTime() - batchStart;
                long sleepNanos = batchIntervalNanos - elapsed;
                if (sleepNanos > 0) {
                    Thread.sleep(sleepNanos / 1_000_000, (int)(sleepNanos % 1_000_000));
                }
                
                // Log de progreso cada 10 segundos
                if (now - lastLogTime >= 10000) {
                    double actualRate = totalCount / ((now - start) / 1000.0);
                    System.out.printf("[Source-%d] Eventos: %,d | Rate: %.0f/s | Objetivo: %d/s%n",
                        subtaskIndex + 1, totalCount, actualRate, eventsPerSecondPerInstance);
                    lastLogTime = now;
                }
            }
            
            // Log final
            long duration = System.currentTimeMillis() - start;
            double finalRate = totalCount / (duration / 1000.0);
            System.out.printf("[Source-%d] FINALIZADO: %,d eventos en %.1fs (%.0f/s)%n",
                subtaskIndex + 1, totalCount, duration / 1000.0, finalRate);
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
    // AGGREGATORS - CORREGIDOS
    // ========================================
    
    /**
     * Sum Aggregator: Suma todos los precios por auctionId
     */
    public static class SumAggregator implements 
            AggregateFunction<Tuple2<Long, Double>, Tuple2<Long, Double>, Tuple2<Long, Double>> {
        
        private static final long serialVersionUID = 1L;
        
        @Override
        public Tuple2<Long, Double> createAccumulator() { 
            return new Tuple2<>(0L, 0.0); 
        }
        
        @Override
        public Tuple2<Long, Double> add(Tuple2<Long, Double> value, Tuple2<Long, Double> acc) {
            return new Tuple2<>(value.f0, acc.f1 + value.f1);
        }
        
        @Override
        public Tuple2<Long, Double> getResult(Tuple2<Long, Double> acc) { 
            return acc; 
        }
        
        @Override
        public Tuple2<Long, Double> merge(Tuple2<Long, Double> a, Tuple2<Long, Double> b) {
            return new Tuple2<>(a.f0, a.f1 + b.f1);
        }
    }
    
    /**
     * Average Aggregator: CORREGIDO para mantener auctionId
     * Usa Tuple3 como acumulador: (auctionId, sum, count)
     */
    public static class AverageAggregator implements 
            AggregateFunction<Tuple2<Long, Double>, Tuple3<Long, Double, Long>, Tuple2<Long, Double>> {
        
        private static final long serialVersionUID = 1L;
        
        @Override
        public Tuple3<Long, Double, Long> createAccumulator() { 
            return new Tuple3<>(0L, 0.0, 0L); // (auctionId, sum, count)
        }
        
        @Override
        public Tuple3<Long, Double, Long> add(Tuple2<Long, Double> value, Tuple3<Long, Double, Long> acc) {
            return new Tuple3<>(value.f0, acc.f1 + value.f1, acc.f2 + 1);
        }
        
        @Override
        public Tuple2<Long, Double> getResult(Tuple3<Long, Double, Long> acc) {
            double average = acc.f2 > 0 ? acc.f1 / acc.f2 : 0.0;
            return new Tuple2<>(acc.f0, average); // (auctionId, average)
        }
        
        @Override
        public Tuple3<Long, Double, Long> merge(Tuple3<Long, Double, Long> a, Tuple3<Long, Double, Long> b) {
            return new Tuple3<>(a.f0, a.f1 + b.f1, a.f2 + b.f2);
        }
    }
    
    /**
     * Count Aggregator: Cuenta eventos por auctionId
     */
    public static class CountAggregator implements 
            AggregateFunction<Tuple2<Long, Double>, Tuple2<Long, Long>, Tuple2<Long, Double>> {
        
        private static final long serialVersionUID = 1L;
        
        @Override
        public Tuple2<Long, Long> createAccumulator() { 
            return new Tuple2<>(0L, 0L); // (auctionId, count)
        }
        
        @Override
        public Tuple2<Long, Long> add(Tuple2<Long, Double> value, Tuple2<Long, Long> acc) {
            return new Tuple2<>(value.f0, acc.f1 + 1);
        }
        
        @Override
        public Tuple2<Long, Double> getResult(Tuple2<Long, Long> acc) { 
            return new Tuple2<>(acc.f0, (double) acc.f1); 
        }
        
        @Override
        public Tuple2<Long, Long> merge(Tuple2<Long, Long> a, Tuple2<Long, Long> b) {
            return new Tuple2<>(a.f0, a.f1 + b.f1);
        }
    }
    
    /**
     * Max Aggregator: Máximo precio por auctionId
     */
    public static class MaxAggregator implements 
            AggregateFunction<Tuple2<Long, Double>, Tuple2<Long, Double>, Tuple2<Long, Double>> {
        
        private static final long serialVersionUID = 1L;
        
        @Override
        public Tuple2<Long, Double> createAccumulator() { 
            return new Tuple2<>(0L, Double.NEGATIVE_INFINITY); // Usar NEGATIVE_INFINITY, no MIN_VALUE
        }
        
        @Override
        public Tuple2<Long, Double> add(Tuple2<Long, Double> value, Tuple2<Long, Double> acc) {
            return new Tuple2<>(value.f0, Math.max(acc.f1, value.f1));
        }
        
        @Override
        public Tuple2<Long, Double> getResult(Tuple2<Long, Double> acc) { 
            return acc; 
        }
        
        @Override
        public Tuple2<Long, Double> merge(Tuple2<Long, Double> a, Tuple2<Long, Double> b) {
            return new Tuple2<>(a.f0, Math.max(a.f1, b.f1));
        }
    }
    
    /**
     * Min Aggregator: Mínimo precio por auctionId
     */
    public static class MinAggregator implements 
            AggregateFunction<Tuple2<Long, Double>, Tuple2<Long, Double>, Tuple2<Long, Double>> {
        
        private static final long serialVersionUID = 1L;
        
        @Override
        public Tuple2<Long, Double> createAccumulator() { 
            return new Tuple2<>(0L, Double.POSITIVE_INFINITY); // Usar POSITIVE_INFINITY
        }
        
        @Override
        public Tuple2<Long, Double> add(Tuple2<Long, Double> value, Tuple2<Long, Double> acc) {
            return new Tuple2<>(value.f0, Math.min(acc.f1, value.f1));
        }
        
        @Override
        public Tuple2<Long, Double> getResult(Tuple2<Long, Double> acc) { 
            return acc; 
        }
        
        @Override
        public Tuple2<Long, Double> merge(Tuple2<Long, Double> a, Tuple2<Long, Double> b) {
            return new Tuple2<>(a.f0, Math.min(a.f1, b.f1));
        }
    }
}