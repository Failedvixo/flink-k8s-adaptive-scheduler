package com.thesis.benchmark.nexmark;

import org.apache.flink.api.common.eventtime.WatermarkStrategy;
import org.apache.flink.api.common.functions.AggregateFunction;
import org.apache.flink.api.common.functions.FilterFunction;
import org.apache.flink.api.common.functions.MapFunction;
import org.apache.flink.api.java.tuple.Tuple2;
import org.apache.flink.streaming.api.datastream.DataStream;
import org.apache.flink.streaming.api.datastream.SingleOutputStreamOperator;
import org.apache.flink.streaming.api.environment.StreamExecutionEnvironment;
import org.apache.flink.streaming.api.functions.windowing.ProcessWindowFunction;
import org.apache.flink.api.common.functions.JoinFunction;
import org.apache.flink.api.java.tuple.Tuple3;
import org.apache.flink.streaming.api.windowing.assigners.EventTimeSessionWindows;
import org.apache.flink.streaming.api.windowing.assigners.ProcessingTimeSessionWindows;
import org.apache.flink.streaming.api.windowing.assigners.SlidingEventTimeWindows;
import org.apache.flink.streaming.api.windowing.assigners.TumblingEventTimeWindows;
import org.apache.flink.streaming.api.windowing.time.Time;
import org.apache.flink.streaming.api.windowing.windows.TimeWindow;
import org.apache.flink.util.Collector;

import java.time.Duration;
import java.util.HashMap;
import java.util.Map;

/**
 * Real Nexmark benchmark dispatcher.
 *
 * Args (positional, matches existing run-experiment-common.sh convention):
 *   0: rate (events/sec, total across streams)
 *   1: durationSec
 *   2: parallelism (operator parallelism for the heavy vertex)
 *   3: window (currently unused — kept for arg-position compatibility)
 *   4: cpuLoad   (currently unused — kept for compatibility)
 *   5: distribution (CONSTANT | SINE | STEP)
 *   6: heavyParallelism (initial parallelism for the heavy vertex)
 *   7: maxEventAgeMs (stale-drop)
 *   8: query   (q5)
 *   9: zipfAlpha (optional, default 0.5)
 *  10: hotAuctionPool (optional, default 1000)
 *
 * The first 8 args mirror ConfigurableGraphJob.fromArgs so the same
 * run-experiment-common.sh works. Args 8+ are Nexmark-specific.
 */
public class NexmarkRealJob {

    public static void main(String[] args) throws Exception {
        if (args.length < 9) {
            System.err.println("Usage: NexmarkRealJob <rate> <duration> <parallelism> <window> <cpuLoad>"
                    + " <distribution> <heavyPar> <maxEventAgeMs> <query> [zipfAlpha] [hotAuctionPool]");
            System.exit(2);
        }
        long rate          = Long.parseLong(args[0]);
        int  durationSec   = Integer.parseInt(args[1]);
        int  parallelism   = Integer.parseInt(args[2]);
        // args[3] window unused
        // args[4] cpuLoad unused
        String dist        = args[5];
        int  heavyPar      = Integer.parseInt(args[6]);
        long maxEventAgeMs = Long.parseLong(args[7]);
        String query       = args[8].toLowerCase();
        double zipfAlpha   = args.length > 9  ? Double.parseDouble(args[9])  : 0.5;
        int hotPool        = args.length > 10 ? Integer.parseInt(args[10])   : 1000;

        System.out.println("==========================================");
        System.out.println("  NEXMARK REAL — query=" + query);
        System.out.println("==========================================");
        System.out.println("  rate=" + rate + " ev/s  duration=" + durationSec + "s  dist=" + dist);
        System.out.println("  heavyPar=" + heavyPar + "  maxAge=" + maxEventAgeMs + "ms");
        System.out.println("  zipfAlpha=" + zipfAlpha + "  hotAuctionPool=" + hotPool);

        StreamExecutionEnvironment env = StreamExecutionEnvironment.getExecutionEnvironment();
        env.setParallelism(parallelism);
        env.disableOperatorChaining();   // so each vertex shows up separately in REST API

        NexmarkGenerator gen = new NexmarkGenerator(
                rate, durationSec, dist,
                /*sineAmplitude*/ 0.7,
                /*stepHighFrac*/  1.5,
                zipfAlpha, maxEventAgeMs, hotPool);

        DataStream<NexmarkEvent> events = env
                .addSource(gen).name("nexmark-source").uid("nexmark-source")
                .assignTimestampsAndWatermarks(
                        WatermarkStrategy.<NexmarkEvent>forBoundedOutOfOrderness(Duration.ofSeconds(2))
                                .withTimestampAssigner((e, ts) -> e.eventTime));

        switch (query) {
            case "q0":  runQ0(events, heavyPar);  break;
            case "q1":  runQ1(events, heavyPar);  break;
            case "q2":  runQ2(events, heavyPar);  break;
            case "q3":  runQ3(events, heavyPar);  break;
            case "q4":  runQ4(events, heavyPar);  break;
            case "q5":  runQ5(events, heavyPar);  break;
            case "q6":  runQ6(events, heavyPar);  break;
            case "q7":  runQ7(events, heavyPar);  break;
            case "q8":  runQ8(events, heavyPar);  break;
            case "q9":  runQ9(events, heavyPar);  break;
            case "q10": runQ10(events, heavyPar); break;
            case "q11": runQ11(events, heavyPar); break;
            case "q12": runQ12(events, heavyPar); break;
            default:
                throw new IllegalArgumentException("Unsupported query: " + query
                        + " (supported: q0..q12)");
        }

        env.execute("Nexmark-" + query.toUpperCase());
    }

    // ----------------------------------------------------------------
    //   Q5 — Hot Items
    //     Over a sliding window of 60s advancing every 5s, find the
    //     auction(s) with the largest bid count.
    //   Standard parameters: window=60min slide=1min; we shrink to
    //   60s/5s for end-to-end benchmark runs that fit a 5-min job.
    // ----------------------------------------------------------------
    private static void runQ5(DataStream<NexmarkEvent> events, int heavyPar) {
        DataStream<Bid> bids = events
                .filter((FilterFunction<NexmarkEvent>) e -> e.type == NexmarkEvent.Type.BID)
                .name("filter-bids").uid("filter-bids")
                .map((MapFunction<NexmarkEvent, Bid>) e -> e.bid)
                .returns(Bid.class)
                .name("project-bid").uid("project-bid");

        // (auctionId, 1) → keyBy(auctionId) → 60s/5s window count
        DataStream<Tuple2<Long, Long>> counts = bids
                .map((MapFunction<Bid, Tuple2<Long, Long>>) b -> Tuple2.of(b.auction, 1L))
                .returns(org.apache.flink.api.common.typeinfo.TypeInformation
                        .of(new org.apache.flink.api.common.typeinfo.TypeHint<Tuple2<Long, Long>>(){}))
                .name("to-tuple").uid("to-tuple")
                .keyBy(t -> t.f0)
                .window(SlidingEventTimeWindows.of(Time.seconds(60), Time.seconds(5)))
                .aggregate(new CountAgg(), new EmitWindowEnd())
                .setParallelism(heavyPar)
                .name("hot-items-count").uid("hot-items-count");

        // Per-window: keep the global top auction.
        counts
                .windowAll(SlidingEventTimeWindows.of(Time.seconds(60), Time.seconds(5)))
                .process(new TopHotAuction())
                .name("top-hot-auction").uid("top-hot-auction")
                .addSink(new HotItemsSink())
                .name("q5-sink").uid("q5-sink");
    }

    // ---------- Q5 helpers ----------

    public static class CountAgg implements AggregateFunction<Tuple2<Long, Long>, Long, Long> {
        @Override public Long createAccumulator() { return 0L; }
        @Override public Long add(Tuple2<Long, Long> v, Long acc) { return acc + v.f1; }
        @Override public Long getResult(Long acc) { return acc; }
        @Override public Long merge(Long a, Long b) { return a + b; }
    }

    /** Re-attach the auction id (from the key) and the window end. */
    public static class EmitWindowEnd
            extends ProcessWindowFunction<Long, Tuple2<Long, Long>, Long, TimeWindow> {
        @Override
        public void process(Long auctionId, Context ctx, Iterable<Long> counts,
                            Collector<Tuple2<Long, Long>> out) {
            for (Long c : counts) {
                out.collect(Tuple2.of(auctionId, c));
            }
        }
    }

    /** Reduce per-window counts to the single hottest auction in the window. */
    public static class TopHotAuction
            extends org.apache.flink.streaming.api.functions.windowing.ProcessAllWindowFunction<
                    Tuple2<Long, Long>, Tuple2<Long, Long>, TimeWindow> {
        @Override
        public void process(Context ctx, Iterable<Tuple2<Long, Long>> elements,
                            Collector<Tuple2<Long, Long>> out) {
            long bestId = -1;
            long bestCount = -1;
            for (Tuple2<Long, Long> t : elements) {
                if (t.f1 > bestCount) { bestCount = t.f1; bestId = t.f0; }
            }
            if (bestId >= 0) out.collect(Tuple2.of(bestId, bestCount));
        }
    }

    /** Lightweight stdout sink that mirrors the existing source-stats style. */
    public static class HotItemsSink
            extends org.apache.flink.streaming.api.functions.sink.RichSinkFunction<Tuple2<Long, Long>> {
        private long received = 0;
        private long lastLog = 0;
        private final Map<Long, Long> lastTopByAuction = new HashMap<>();

        @Override
        public void invoke(Tuple2<Long, Long> v, Context ctx) {
            received++;
            lastTopByAuction.merge(v.f0, 1L, Long::sum);
            long now = System.currentTimeMillis();
            if (now - lastLog > 5000) {
                lastLog = now;
                System.out.printf("[Q5-Sink-%d] received=%,d topAuction=%d count=%,d uniqueWinners=%d%n",
                        getRuntimeContext().getIndexOfThisSubtask() + 1,
                        received, v.f0, v.f1, lastTopByAuction.size());
            }
        }
    }

    // ----------------------------------------------------------------
    //   Q8 — Monitor New Users
    //     Over a 10s tumbling event-time window, emit (person, auction)
    //     pairs where the same person both joined the system AND
    //     created an auction during the window.
    //   We model this as an inner join on  person.id == auction.seller
    //   inside the same window.
    // ----------------------------------------------------------------
    private static void runQ8(DataStream<NexmarkEvent> events, int heavyPar) {
        DataStream<Person> persons = events
                .filter((FilterFunction<NexmarkEvent>) e -> e.type == NexmarkEvent.Type.PERSON)
                .name("filter-persons").uid("filter-persons")
                .map((MapFunction<NexmarkEvent, Person>) e -> e.person)
                .returns(Person.class)
                .name("project-person").uid("project-person");

        DataStream<Auction> auctions = events
                .filter((FilterFunction<NexmarkEvent>) e -> e.type == NexmarkEvent.Type.AUCTION)
                .name("filter-auctions").uid("filter-auctions")
                .map((MapFunction<NexmarkEvent, Auction>) e -> e.auction)
                .returns(Auction.class)
                .name("project-auction").uid("project-auction");

        // JoinedStreams.WithWindow.apply() declares DataStream<T> but the runtime
        // instance is SingleOutputStreamOperator<T> — cast it back to recover
        // .setParallelism()/.name()/.uid(). The TypeInformation goes in apply()
        // directly (lambda erases the generic, so .returns() afterward won't compile).
        SingleOutputStreamOperator<Tuple3<Long, Long, String>> newUsers =
                (SingleOutputStreamOperator<Tuple3<Long, Long, String>>) persons
                        .join(auctions)
                        .where((org.apache.flink.api.java.functions.KeySelector<Person, Long>) p -> p.id)
                        .equalTo((org.apache.flink.api.java.functions.KeySelector<Auction, Long>) a -> a.seller)
                        .window(TumblingEventTimeWindows.of(Time.seconds(10)))
                        .apply(
                                (JoinFunction<Person, Auction, Tuple3<Long, Long, String>>)
                                        (p, a) -> Tuple3.of(p.id, a.id, p.name),
                                org.apache.flink.api.common.typeinfo.TypeInformation
                                        .of(new org.apache.flink.api.common.typeinfo.TypeHint<Tuple3<Long, Long, String>>(){}));

        newUsers
                .setParallelism(heavyPar)
                .name("new-users-join").uid("new-users-join");

        newUsers
                .addSink(new NewUsersSink())
                .name("q8-sink").uid("q8-sink");
    }

    /** Stdout sink for Q8 — periodic stats by sub-task. */
    public static class NewUsersSink
            extends org.apache.flink.streaming.api.functions.sink.RichSinkFunction<Tuple3<Long, Long, String>> {
        private long received = 0;
        private long lastLog = 0;

        @Override
        public void invoke(Tuple3<Long, Long, String> v, Context ctx) {
            received++;
            long now = System.currentTimeMillis();
            if (now - lastLog > 5000) {
                lastLog = now;
                System.out.printf("[Q8-Sink-%d] received=%,d lastPair=(person=%d,auction=%d,name=%s)%n",
                        getRuntimeContext().getIndexOfThisSubtask() + 1,
                        received, v.f0, v.f1, v.f2);
            }
        }
    }

    // ================================================================
    //   Q0 — Identity pass-through (baseline cost of streaming pipeline)
    // ================================================================
    private static void runQ0(DataStream<NexmarkEvent> events, int heavyPar) {
        events.map((MapFunction<NexmarkEvent, NexmarkEvent>) e -> e)
                .returns(NexmarkEvent.class)
                .setParallelism(heavyPar)
                .name("q0-passthrough").uid("q0-passthrough")
                .addSink(new GenericCountSink<>("Q0"))
                .name("q0-sink").uid("q0-sink");
    }

    // ================================================================
    //   Q1 — Currency conversion: bid.price USD → EUR (rate 0.908)
    // ================================================================
    private static void runQ1(DataStream<NexmarkEvent> events, int heavyPar) {
        events.filter((FilterFunction<NexmarkEvent>) e -> e.type == NexmarkEvent.Type.BID)
                .name("filter-bids-q1").uid("filter-bids-q1")
                .map((MapFunction<NexmarkEvent, Bid>) e -> {
                    Bid out = new Bid();
                    out.auction  = e.bid.auction;
                    out.bidder   = e.bid.bidder;
                    out.price    = (long) (e.bid.price * 0.908);  // USD → EUR
                    out.channel  = e.bid.channel;
                    out.dateTime = e.bid.dateTime;
                    return out;
                })
                .returns(Bid.class)
                .setParallelism(heavyPar)
                .name("q1-currency").uid("q1-currency")
                .addSink(new GenericCountSink<>("Q1"))
                .name("q1-sink").uid("q1-sink");
    }

    // ================================================================
    //   Q2 — Selection on auction id (~0.8% of bids pass through)
    // ================================================================
    private static void runQ2(DataStream<NexmarkEvent> events, int heavyPar) {
        events.filter((FilterFunction<NexmarkEvent>) e -> e.type == NexmarkEvent.Type.BID)
                .name("filter-bids-q2").uid("filter-bids-q2")
                .filter((FilterFunction<NexmarkEvent>) e -> e.bid.auction % 123 == 0)
                .setParallelism(heavyPar)
                .name("q2-selection").uid("q2-selection")
                .addSink(new GenericCountSink<>("Q2"))
                .name("q2-sink").uid("q2-sink");
    }

    // ================================================================
    //   Q3 — Local item suggestion: Person ⨝ Auction, state ∈ {OR,ID,CA}
    //   60s tumbling event-time inner join.
    // ================================================================
    private static void runQ3(DataStream<NexmarkEvent> events, int heavyPar) {
        DataStream<Person> persons = events
                .filter((FilterFunction<NexmarkEvent>) e -> e.type == NexmarkEvent.Type.PERSON)
                .name("filter-persons-q3").uid("filter-persons-q3")
                .filter((FilterFunction<NexmarkEvent>) e -> {
                    String st = e.person.state;
                    return "OR".equals(st) || "ID".equals(st) || "CA".equals(st);
                })
                .name("filter-state-q3").uid("filter-state-q3")
                .map((MapFunction<NexmarkEvent, Person>) e -> e.person)
                .returns(Person.class)
                .name("project-person-q3").uid("project-person-q3");

        DataStream<Auction> auctions = events
                .filter((FilterFunction<NexmarkEvent>) e -> e.type == NexmarkEvent.Type.AUCTION)
                .name("filter-auctions-q3").uid("filter-auctions-q3")
                .map((MapFunction<NexmarkEvent, Auction>) e -> e.auction)
                .returns(Auction.class)
                .name("project-auction-q3").uid("project-auction-q3");

        SingleOutputStreamOperator<Tuple3<String, String, Long>> joined =
                (SingleOutputStreamOperator<Tuple3<String, String, Long>>) persons
                        .join(auctions)
                        .where((org.apache.flink.api.java.functions.KeySelector<Person, Long>) p -> p.id)
                        .equalTo((org.apache.flink.api.java.functions.KeySelector<Auction, Long>) a -> a.seller)
                        .window(TumblingEventTimeWindows.of(Time.seconds(60)))
                        .apply(
                                (JoinFunction<Person, Auction, Tuple3<String, String, Long>>)
                                        (p, a) -> Tuple3.of(p.name, p.city, a.id),
                                org.apache.flink.api.common.typeinfo.TypeInformation
                                        .of(new org.apache.flink.api.common.typeinfo.TypeHint<Tuple3<String, String, Long>>(){}));

        joined.setParallelism(heavyPar)
                .name("q3-state-join").uid("q3-state-join")
                .addSink(new GenericCountSink<>("Q3"))
                .name("q3-sink").uid("q3-sink");
    }

    // ================================================================
    //   Q4 — Average price per category (proxy: auction id % 16)
    //   Per-key 60s tumbling event-time avg.
    // ================================================================
    private static void runQ4(DataStream<NexmarkEvent> events, int heavyPar) {
        DataStream<Tuple2<Long, Long>> catPrice = events
                .filter((FilterFunction<NexmarkEvent>) e -> e.type == NexmarkEvent.Type.BID)
                .name("filter-bids-q4").uid("filter-bids-q4")
                .map((MapFunction<NexmarkEvent, Tuple2<Long, Long>>) e ->
                        Tuple2.of(e.bid.auction % 16, e.bid.price))
                .returns(org.apache.flink.api.common.typeinfo.TypeInformation
                        .of(new org.apache.flink.api.common.typeinfo.TypeHint<Tuple2<Long, Long>>(){}))
                .name("to-cat-price-q4").uid("to-cat-price-q4");

        catPrice
                .keyBy(t -> t.f0)
                .window(TumblingEventTimeWindows.of(Time.seconds(60)))
                .aggregate(new AvgPriceAgg(), new EmitAvgPerKey())
                .setParallelism(heavyPar)
                .name("q4-cat-avg").uid("q4-cat-avg")
                .addSink(new GenericCountSink<>("Q4"))
                .name("q4-sink").uid("q4-sink");
    }

    // ================================================================
    //   Q6 — Average selling price by seller proxy (bidder % 256).
    //   Per-key 60s tumbling event-time avg.
    // ================================================================
    private static void runQ6(DataStream<NexmarkEvent> events, int heavyPar) {
        DataStream<Tuple2<Long, Long>> sellerPrice = events
                .filter((FilterFunction<NexmarkEvent>) e -> e.type == NexmarkEvent.Type.BID)
                .name("filter-bids-q6").uid("filter-bids-q6")
                .map((MapFunction<NexmarkEvent, Tuple2<Long, Long>>) e ->
                        Tuple2.of(e.bid.bidder % 256, e.bid.price))
                .returns(org.apache.flink.api.common.typeinfo.TypeInformation
                        .of(new org.apache.flink.api.common.typeinfo.TypeHint<Tuple2<Long, Long>>(){}))
                .name("to-seller-price-q6").uid("to-seller-price-q6");

        sellerPrice
                .keyBy(t -> t.f0)
                .window(TumblingEventTimeWindows.of(Time.seconds(60)))
                .aggregate(new AvgPriceAgg(), new EmitAvgPerKey())
                .setParallelism(heavyPar)
                .name("q6-seller-avg").uid("q6-seller-avg")
                .addSink(new GenericCountSink<>("Q6"))
                .name("q6-sink").uid("q6-sink");
    }

    // ================================================================
    //   Q7 — Highest bid in a 10s tumbling event-time window (global).
    // ================================================================
    private static void runQ7(DataStream<NexmarkEvent> events, int heavyPar) {
        // windowAll(...) forces parallelism=1 on the window operator, so the
        // heavy vertex sits in the filter+map stage that feeds it. The autoscaler
        // pattern 'q7-max-bid' matches the parallelisable upstream stage.
        DataStream<Bid> bids = events
                .filter((FilterFunction<NexmarkEvent>) e -> e.type == NexmarkEvent.Type.BID)
                .name("filter-bids-q7").uid("filter-bids-q7")
                .map((MapFunction<NexmarkEvent, Bid>) e -> e.bid)
                .returns(Bid.class)
                .setParallelism(heavyPar)
                .name("q7-max-bid").uid("q7-max-bid");

        bids.windowAll(TumblingEventTimeWindows.of(Time.seconds(10)))
                .max("price")
                .name("q7-global-max").uid("q7-global-max")
                .addSink(new GenericCountSink<>("Q7"))
                .name("q7-sink").uid("q7-sink");
    }

    // ================================================================
    //   Q9 — Winning bid per auction over 60s tumbling event-time window.
    // ================================================================
    private static void runQ9(DataStream<NexmarkEvent> events, int heavyPar) {
        DataStream<Tuple2<Long, Long>> auctionPrice = events
                .filter((FilterFunction<NexmarkEvent>) e -> e.type == NexmarkEvent.Type.BID)
                .name("filter-bids-q9").uid("filter-bids-q9")
                .map((MapFunction<NexmarkEvent, Tuple2<Long, Long>>) e ->
                        Tuple2.of(e.bid.auction, e.bid.price))
                .returns(org.apache.flink.api.common.typeinfo.TypeInformation
                        .of(new org.apache.flink.api.common.typeinfo.TypeHint<Tuple2<Long, Long>>(){}))
                .name("to-auction-price-q9").uid("to-auction-price-q9");

        auctionPrice
                .keyBy(t -> t.f0)
                .window(TumblingEventTimeWindows.of(Time.seconds(60)))
                .reduce((a, b) -> a.f1 >= b.f1 ? a : b)
                .setParallelism(heavyPar)
                .name("q9-winning-bid").uid("q9-winning-bid")
                .addSink(new GenericCountSink<>("Q9"))
                .name("q9-sink").uid("q9-sink");
    }

    // ================================================================
    //   Q10 — Log to sink: high-throughput sink-bound workload.
    // ================================================================
    private static void runQ10(DataStream<NexmarkEvent> events, int heavyPar) {
        events.map((MapFunction<NexmarkEvent, NexmarkEvent>) e -> e)
                .returns(NexmarkEvent.class)
                .setParallelism(heavyPar)
                .name("q10-sink").uid("q10-sink")
                .addSink(new GenericCountSink<>("Q10"))
                .name("q10-final-sink").uid("q10-final-sink");
    }

    // ================================================================
    //   Q11 — Per-bidder event-time session windows (gap 10s).
    //   Counts bids per user session.
    // ================================================================
    private static void runQ11(DataStream<NexmarkEvent> events, int heavyPar) {
        DataStream<Tuple2<Long, Long>> bidderOne = events
                .filter((FilterFunction<NexmarkEvent>) e -> e.type == NexmarkEvent.Type.BID)
                .name("filter-bids-q11").uid("filter-bids-q11")
                .map((MapFunction<NexmarkEvent, Tuple2<Long, Long>>) e ->
                        Tuple2.of(e.bid.bidder, 1L))
                .returns(org.apache.flink.api.common.typeinfo.TypeInformation
                        .of(new org.apache.flink.api.common.typeinfo.TypeHint<Tuple2<Long, Long>>(){}))
                .name("to-bidder-one-q11").uid("to-bidder-one-q11");

        bidderOne
                .keyBy(t -> t.f0)
                .window(EventTimeSessionWindows.withGap(Time.seconds(10)))
                .aggregate(new CountAgg(), new EmitWindowEnd())
                .setParallelism(heavyPar)
                .name("q11-sessions").uid("q11-sessions")
                .addSink(new GenericCountSink<>("Q11"))
                .name("q11-sink").uid("q11-sink");
    }

    // ================================================================
    //   Q12 — Same as Q11 but processing-time session windows.
    //   Captures wall-clock burstiness instead of event-time.
    // ================================================================
    private static void runQ12(DataStream<NexmarkEvent> events, int heavyPar) {
        DataStream<Tuple2<Long, Long>> bidderOne = events
                .filter((FilterFunction<NexmarkEvent>) e -> e.type == NexmarkEvent.Type.BID)
                .name("filter-bids-q12").uid("filter-bids-q12")
                .map((MapFunction<NexmarkEvent, Tuple2<Long, Long>>) e ->
                        Tuple2.of(e.bid.bidder, 1L))
                .returns(org.apache.flink.api.common.typeinfo.TypeInformation
                        .of(new org.apache.flink.api.common.typeinfo.TypeHint<Tuple2<Long, Long>>(){}))
                .name("to-bidder-one-q12").uid("to-bidder-one-q12");

        bidderOne
                .keyBy(t -> t.f0)
                .window(ProcessingTimeSessionWindows.withGap(Time.seconds(10)))
                .aggregate(new CountAgg(), new EmitWindowEnd())
                .setParallelism(heavyPar)
                .name("q12-proc-sessions").uid("q12-proc-sessions")
                .addSink(new GenericCountSink<>("Q12"))
                .name("q12-sink").uid("q12-sink");
    }

    // ================================================================
    //   Shared helpers used by the Q0–Q12 implementations above
    // ================================================================

    /** Generic count-and-print sink labelled by query. */
    public static class GenericCountSink<T>
            extends org.apache.flink.streaming.api.functions.sink.RichSinkFunction<T> {
        private final String label;
        private long received = 0;
        private long lastLog = 0;

        public GenericCountSink(String label) {
            this.label = label;
        }

        @Override
        public void invoke(T v, Context ctx) {
            received++;
            long now = System.currentTimeMillis();
            if (now - lastLog > 5000) {
                lastLog = now;
                String repr = String.valueOf(v);
                if (repr.length() > 80) repr = repr.substring(0, 77) + "...";
                System.out.printf("[%s-Sink-%d] received=%,d last=%s%n",
                        label,
                        getRuntimeContext().getIndexOfThisSubtask() + 1,
                        received, repr);
            }
        }
    }

    /** Accumulator for windowed average: (sum, count). */
    public static class AvgPriceAcc implements java.io.Serializable {
        public long sum = 0;
        public long count = 0;
    }

    /** AggregateFunction for average price over (key, price) pairs. */
    public static class AvgPriceAgg
            implements AggregateFunction<Tuple2<Long, Long>, AvgPriceAcc, Double> {
        @Override public AvgPriceAcc createAccumulator() { return new AvgPriceAcc(); }
        @Override public AvgPriceAcc add(Tuple2<Long, Long> v, AvgPriceAcc acc) {
            acc.sum += v.f1; acc.count++; return acc;
        }
        @Override public Double getResult(AvgPriceAcc acc) {
            return acc.count == 0 ? 0.0 : (double) acc.sum / acc.count;
        }
        @Override public AvgPriceAcc merge(AvgPriceAcc a, AvgPriceAcc b) {
            AvgPriceAcc m = new AvgPriceAcc();
            m.sum = a.sum + b.sum; m.count = a.count + b.count; return m;
        }
    }

    /** Emit (key, avg) after the keyed AvgPriceAgg aggregate. */
    public static class EmitAvgPerKey
            extends ProcessWindowFunction<Double, Tuple2<Long, Double>, Long, TimeWindow> {
        @Override
        public void process(Long key, Context ctx, Iterable<Double> avgs,
                            Collector<Tuple2<Long, Double>> out) {
            for (Double a : avgs) out.collect(Tuple2.of(key, a));
        }
    }
}
