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
            case "q5": runQ5(events, heavyPar); break;
            case "q8": runQ8(events, heavyPar); break;
            default:
                throw new IllegalArgumentException("Unsupported query: " + query
                        + " (supported: q5, q8)");
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
}
