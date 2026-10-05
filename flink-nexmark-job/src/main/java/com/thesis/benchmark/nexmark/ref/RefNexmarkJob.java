package com.thesis.benchmark.nexmark.ref;

import com.thesis.benchmark.GraphConfig;
import org.apache.beam.sdk.nexmark.model.Auction;
import org.apache.beam.sdk.nexmark.model.Bid;
import org.apache.beam.sdk.nexmark.model.Person;
import org.apache.flink.api.common.eventtime.WatermarkStrategy;
import org.apache.flink.api.common.functions.AggregateFunction;
import org.apache.flink.api.common.functions.JoinFunction;
import org.apache.flink.api.common.state.ListState;
import org.apache.flink.api.common.state.ListStateDescriptor;
import org.apache.flink.api.common.state.ValueState;
import org.apache.flink.api.common.state.ValueStateDescriptor;
import org.apache.flink.api.common.typeinfo.TypeHint;
import org.apache.flink.api.common.typeinfo.TypeInformation;
import org.apache.flink.api.java.functions.KeySelector;
import org.apache.flink.api.java.tuple.Tuple3;
import org.apache.flink.api.java.tuple.Tuple4;
import org.apache.flink.streaming.api.datastream.DataStream;
import org.apache.flink.streaming.api.datastream.DataStreamSink;
import org.apache.flink.streaming.api.datastream.SingleOutputStreamOperator;
import org.apache.flink.streaming.api.environment.StreamExecutionEnvironment;
import org.apache.flink.streaming.api.functions.KeyedProcessFunction;
import org.apache.flink.streaming.api.functions.co.KeyedCoProcessFunction;
import org.apache.flink.streaming.api.functions.windowing.ProcessWindowFunction;
import org.apache.flink.streaming.api.functions.sink.legacy.RichSinkFunction;
import org.apache.flink.streaming.api.windowing.assigners.SlidingEventTimeWindows;
import org.apache.flink.streaming.api.windowing.assigners.TumblingEventTimeWindows;
import org.apache.flink.streaming.api.windowing.windows.TimeWindow;
import org.apache.flink.util.Collector;

import java.time.Duration;

/**
 * Nexmark on the REFERENCE event generator.
 *
 * <p>Same positional argument contract as {@link com.thesis.benchmark.nexmark.NexmarkRealJob},
 * so the harness switches to it by setting JOB_CLASS and nothing else:
 *
 * <pre>
 *   0: rate (events/s, total across the streams this query uses)
 *   1: durationSec        5: distribution        8: query
 *   2: parallelism        6: heavyParallelism    9: zipfAlpha (unused here)
 *   3: window (unused)    7: maxEventAgeMs (unused here)   10: hotAuctionPool (unused here)
 *   4: cpuLoad (unused)                          11: slot sharing mode
 * </pre>
 *
 * <p>Args 9 and 10 shape the project's own generator and have no counterpart here:
 * skew and hot-pool size are properties of Beam's {@code NexmarkConfiguration}, and
 * overriding them from the command line would be a second, undocumented generator
 * configuration sitting beside the reference one. They are accepted and ignored so
 * the argument positions stay compatible.
 *
 * <p><b>Why this exists rather than another branch inside NexmarkRealJob.</b> That
 * job builds one tagged stream and filters it per query; this one has a source per
 * event type, which is how the reference implementation is organised and what lets
 * the join see full-rate inputs instead of the 8% that survived the filters.
 */
public class RefNexmarkJob {

    private static GraphConfig.SlotSharingMode slotSharing = GraphConfig.SlotSharingMode.SHARED;

    public static void main(String[] args) throws Exception {
        if (args.length < 9) {
            System.err.println("Usage: RefNexmarkJob <rate> <duration> <parallelism> <window>"
                    + " <cpuLoad> <distribution> <heavyPar> <maxEventAgeMs> <query>"
                    + " [zipfAlpha] [hotPool] [slotSharing]");
            System.exit(2);
        }
        final int rate = Integer.parseInt(args[0]);
        final int durationSec = Integer.parseInt(args[1]);
        final int parallelism = Integer.parseInt(args[2]);
        final String dist = args[5];
        final int heavyPar = Integer.parseInt(args[6]);
        final long maxEventAgeMs = Long.parseLong(args[7]);
        final String query = args[8].toLowerCase();
        slotSharing = args.length > 11 && !args[11].isEmpty()
                ? GraphConfig.SlotSharingMode.valueOf(args[11].toUpperCase())
                : GraphConfig.SlotSharingMode.SHARED;

        if (!"CONSTANT".equalsIgnoreCase(dist)) {
            // Beam's generator emits at a fixed rate. Accepting SINE or STEP here would
            // silently run a constant load under a label that says otherwise, which is
            // worse than refusing: every campaign records the distribution it asked for.
            throw new IllegalArgumentException(
                    "the reference generator only produces a constant rate; got dist=" + dist);
        }

        System.out.println("==========================================");
        System.out.println("  NEXMARK (reference generator) — query=" + query);
        System.out.println("==========================================");
        System.out.println("  rate=" + rate + " ev/s  duration=" + durationSec + "s");
        System.out.println("  parallelism=" + parallelism + "  heavyPar=" + heavyPar);
        System.out.println("  slot sharing=" + slotSharing
                + "  maxEventAge=" + maxEventAgeMs + "ms"
                + (maxEventAgeMs > 0 ? " (descarta en vez de encolar)" : " (sin descarte)"));

        final StreamExecutionEnvironment env = StreamExecutionEnvironment.getExecutionEnvironment();
        env.setParallelism(parallelism);
        // Without this Flink's default chaining collapses most Nexmark queries into a
        // single vertex, leaving nothing to place — CAPSys disables it for the same
        // reason (EuroSys'25 §6.1).
        env.disableOperatorChaining();

        if ("q8".equals(query)) {
            runQ8(env, rate, durationSec, heavyPar, maxEventAgeMs);
        } else if ("q5".equals(query)) {
            runQ5(env, rate, durationSec, heavyPar, maxEventAgeMs);
        } else if ("q3".equals(query)) {
            runQ3(env, rate, durationSec, heavyPar, maxEventAgeMs, q3StateTtlSec());
        } else {
            throw new IllegalArgumentException(
                    "only q3, q5 and q8 are ported to the reference sources so far, got " + query);
        }

        env.execute("nexmark-ref-" + query + " rate=" + rate + " par=" + parallelism
                + " share=" + slotSharing);
    }

    /** Assigns an operator to its slot sharing group; see GraphConfig.SlotSharingMode. */
    private static <T> SingleOutputStreamOperator<T> g(
            SingleOutputStreamOperator<T> op, String stage, String vertex) {
        switch (slotSharing) {
            case PER_STAGE:    return op.slotSharingGroup(stage);
            case PER_OPERATOR: return op.slotSharingGroup(vertex);
            default:           return op;
        }
    }

    // ================================================================
    //   Q8 — Monitor New Users
    //     Persons who joined AND opened an auction inside the same
    //     10-second tumbling window, joined on person.id == auction.seller.
    // ================================================================
    private static void runQ8(StreamExecutionEnvironment env, int rate, int durationSec,
                              int heavyPar, long maxEventAgeMs) {
        // THE RATE SPLIT IS THE CANONICAL PROPORTION, not an arbitrary one. Nexmark
        // generates persons, auctions and bids at 1:3:46; this query consumes no bids,
        // so of the traffic it does consume, one part in four is a person. DS2's port
        // hardcodes 30k persons and 50k auctions, which is neither the canonical ratio
        // nor tied to a requested rate — fine for their purposes, wrong for a campaign
        // whose operating point is a calibrated events/second figure.
        final int personRate = Math.max(1, rate / 4);
        // EXACTLY three auctions per person (2026-09-18): both sources walk Beam's shared event
        // sequence at the same epoch rate, one person and three auctions per epoch, so the two
        // rates must be in Beam's 1:3 proportion to the unit. `rate - personRate` drifted off it
        // whenever the rate was not a multiple of four, and a drifting epoch rate is exactly
        // how the two sources used to fall out of step. The total is rounded down to 4x.
        final int auctionRate = 3 * personRate;

        final DataStream<Person> persons = g(env
                .addSource(new RefPersonSource(personRate, durationSec, maxEventAgeMs))
                .name("person-source").uid("person-source"), "person", "person-source")
                .assignTimestampsAndWatermarks(
                        WatermarkStrategy.<Person>forBoundedOutOfOrderness(Duration.ofSeconds(2))
                                .withTimestampAssigner((p, ts) -> p.dateTime.getMillis()))
                .name("person-watermarks").uid("person-watermarks");

        final DataStream<Auction> auctions = g(env
                .addSource(new RefAuctionSource(auctionRate, durationSec, maxEventAgeMs))
                .name("auction-source").uid("auction-source"), "auction", "auction-source")
                .assignTimestampsAndWatermarks(
                        WatermarkStrategy.<Auction>forBoundedOutOfOrderness(Duration.ofSeconds(2))
                                .withTimestampAssigner((a, ts) -> a.dateTime.getMillis()))
                .name("auction-watermarks").uid("auction-watermarks");

        // JoinedStreams.WithWindow.apply() is declared to return DataStream<T> while the
        // runtime object is a SingleOutputStreamOperator<T>; the cast is what recovers
        // setParallelism/name/uid. The TypeInformation goes inside apply() because the
        // lambda erases the generic and a trailing .returns() will not compile.
        @SuppressWarnings("unchecked")
        final SingleOutputStreamOperator<Tuple3<Long, String, Long>> newUsers =
                (SingleOutputStreamOperator<Tuple3<Long, String, Long>>) persons
                        .join(auctions)
                        .where((KeySelector<Person, Long>) p -> p.id)
                        .equalTo((KeySelector<Auction, Long>) a -> a.seller)
                        .window(TumblingEventTimeWindows.of(Duration.ofSeconds(10)))
                        .apply((JoinFunction<Person, Auction, Tuple3<Long, String, Long>>)
                                        (p, a) -> Tuple3.of(p.id, p.name, a.reserve),
                                org.apache.flink.api.common.typeinfo.TypeInformation.of(
                                        new org.apache.flink.api.common.typeinfo.TypeHint<
                                                Tuple3<Long, String, Long>>() {}));

        // The operator the whole exercise is about: state- and memory-bound rather than
        // CPU-bound, so the machine that suits it is not the one a scalar load model
        // would pick. Its RocksDB budget is the managed memory of whichever slot it
        // lands on — 96 MB on `fast`, 32 MB on `slow`.
        g(newUsers.setParallelism(heavyPar).name("new-users-join").uid("new-users-join"),
                "join", "new-users-join");

        final DataStreamSink<Tuple3<Long, String, Long>> sink = newUsers
                .addSink(new NewUsersSink()).name("q8-sink").uid("q8-sink");
        if (slotSharing != GraphConfig.SlotSharingMode.SHARED) {
            sink.slotSharingGroup("snk");
        }
    }

    // ================================================================
    //   Q3 — Local Item Suggestion
    //     Sellers from OR, ID or CA joined with their auctions in
    //     category 10, on person.id == auction.seller. Not windowed:
    //     a person is kept for maxAuctionsWaitingTime (Beam default
    //     600 s) waiting for auctions, and an auction that arrives
    //     before its seller waits for the same time.
    // ================================================================
    //
    // WHY Q3 (2026-10-04). The generalisation test needs an operator whose STATE reaches the
    // disk, and Q5's incremental count did not: its pilot ran the count on the capped-disk
    // machine and on the healthy one at the same 60000 rec/s with no difference, because a
    // counter per auction is rewritten in RocksDB's memtable and almost never flushed. Q3 keeps
    // whole person records for ten minutes — about 230 bytes each, half of all persons — which
    // is state that has to be written out. Its operator is still a different one from Q8's:
    // a keyed two-input process function with expiry timers instead of a windowed join.
    //
    // Same graph shape as Q8 under PER_STAGE sharing: persons (source + filter), auctions
    // (source + filter), join, sink — eight slices at parallelism 2.
    static final java.util.Set<String> Q3_STATES = java.util.Set.of("OR", "ID", "CA");
    static final long Q3_CATEGORY = 10L;

    /** Beam's maxAuctionsWaitingTime unless Q3_STATE_TTL_SEC overrides it (seconds). */
    static long q3StateTtlSec() {
        final String env = System.getenv("Q3_STATE_TTL_SEC");
        if (env != null && !env.isEmpty()) {
            return Long.parseLong(env);
        }
        return org.apache.beam.sdk.nexmark.NexmarkConfiguration.DEFAULT.maxAuctionsWaitingTime;
    }

    private static void runQ3(StreamExecutionEnvironment env, int rate, int durationSec,
                              int heavyPar, long maxEventAgeMs, long ttlSec) {
        System.out.println("  q3: estado de cada persona/subasta pendiente expira a los " + ttlSec + " s");
        // Same canonical 1:3 split as Q8, and for the same reason (see runQ8).
        final int personRate = Math.max(1, rate / 4);
        final int auctionRate = 3 * personRate;

        final DataStream<Person> sellers = g(env
                .addSource(new RefPersonSource(personRate, durationSec, maxEventAgeMs))
                .name("person-source").uid("person-source"), "person", "person-source")
                .filter(p -> Q3_STATES.contains(p.state))
                .name("person-filter").uid("person-filter");

        final DataStream<Auction> category10 = g(env
                .addSource(new RefAuctionSource(auctionRate, durationSec, maxEventAgeMs))
                .name("auction-source").uid("auction-source"), "auction", "auction-source")
                .filter(a -> a.category == Q3_CATEGORY)
                .name("auction-filter").uid("auction-filter");

        final SingleOutputStreamOperator<Tuple4<String, String, String, Long>> joined = sellers
                .keyBy((KeySelector<Person, Long>) p -> p.id)
                .connect(category10.keyBy((KeySelector<Auction, Long>) a -> a.seller))
                .process(new LocalItemJoin(ttlSec * 1000L))
                .returns(TypeInformation.of(
                        new TypeHint<Tuple4<String, String, String, Long>>() {}));
        g(joined.setParallelism(heavyPar).name("q3-state-join").uid("q3-state-join"),
                "join", "q3-state-join");

        final DataStreamSink<Tuple4<String, String, String, Long>> sink = joined
                .addSink(new LocalItemSink()).name("q3-sink").uid("q3-sink");
        if (slotSharing != GraphConfig.SlotSharingMode.SHARED) {
            sink.slotSharingGroup("snk");
        }
    }

    /**
     * Beam's Query3 JoinDoFn in Flink terms. Per seller id: the person, once seen, is kept and
     * every auction of theirs is emitted as it arrives; auctions that come first wait in a list
     * and are emitted when the person shows up. Both expire after the TTL, measured in
     * processing time — event time here IS the wall clock (NexmarkEpochs), and processing-time
     * timers spare the graph two watermark operators Q3 otherwise has no use for.
     */
    public static class LocalItemJoin extends KeyedCoProcessFunction<
            Long, Person, Auction, Tuple4<String, String, String, Long>> {
        private static final long serialVersionUID = 1L;
        private final long ttlMs;
        private transient ValueState<Person> person;
        private transient ListState<Auction> pending;
        private transient ValueState<Long> expiry;

        public LocalItemJoin(long ttlMs) {
            this.ttlMs = ttlMs;
        }

        @Override
        public void open(org.apache.flink.api.common.functions.OpenContext ctx) {
            person = getRuntimeContext().getState(
                    new ValueStateDescriptor<>("person", TypeInformation.of(Person.class)));
            pending = getRuntimeContext().getListState(
                    new ListStateDescriptor<>("pending", TypeInformation.of(Auction.class)));
            expiry = getRuntimeContext().getState(
                    new ValueStateDescriptor<>("expiry", TypeInformation.of(Long.class)));
        }

        private void armExpiry(Context ctx) throws Exception {
            if (expiry.value() == null) {
                final long at = ctx.timerService().currentProcessingTime() + ttlMs;
                ctx.timerService().registerProcessingTimeTimer(at);
                expiry.update(at);
            }
        }

        @Override
        public void processElement1(Person p, Context ctx,
                                    Collector<Tuple4<String, String, String, Long>> out)
                throws Exception {
            person.update(p);
            for (Auction a : pending.get()) {
                out.collect(Tuple4.of(p.name, p.city, p.state, a.id));
            }
            pending.clear();
            armExpiry(ctx);
        }

        @Override
        public void processElement2(Auction a, Context ctx,
                                    Collector<Tuple4<String, String, String, Long>> out)
                throws Exception {
            final Person p = person.value();
            if (p != null) {
                out.collect(Tuple4.of(p.name, p.city, p.state, a.id));
            } else {
                pending.add(a);
                armExpiry(ctx);
            }
        }

        @Override
        public void onTimer(long ts, OnTimerContext ctx,
                            Collector<Tuple4<String, String, String, Long>> out) {
            person.clear();
            pending.clear();
            expiry.clear();
        }
    }

    /** Counts suggestions, so a Q3 that matches nothing is visible. */
    public static class LocalItemSink
            extends RichSinkFunction<Tuple4<String, String, String, Long>> {
        private static final long serialVersionUID = 1L;
        private long received = 0;
        private long lastLog = 0;

        @Override
        public void invoke(Tuple4<String, String, String, Long> v, Context ctx) {
            received++;
            final long now = System.currentTimeMillis();
            if (now - lastLog > 5000) {
                lastLog = now;
                System.out.printf("[Q3-Sink-%d] matches=%,d last=(%s,%s,%s,auction=%d)%n",
                        getRuntimeContext().getTaskInfo().getIndexOfThisSubtask() + 1,
                        received, v.f0, v.f1, v.f2, v.f3);
            }
        }
    }

    // ================================================================
    //   Q5 — Hot Items
    //     Which auctions received the most bids in the last 10 seconds,
    //     re-evaluated every 5 seconds (Beam's NexmarkConfiguration
    //     defaults: windowSizeSec=10, windowPeriodSec=5).
    // ================================================================
    //
    // WHY Q5 (2026-10-04). It is the generalisation test for the placement agent: keyed window
    // state like Q8's join, but held by a DIFFERENT operator — a one-input sliding-window
    // aggregation instead of a two-input join. The agent never sees operator names, only
    // load/disk profiles, so a table trained on Q8 and frozen should place this operator by
    // what it does. Whether it writes enough to disk for a capped disk to matter is an
    // empirical question the pilot answers before any campaign.
    //
    // The graph has the same shape as Q8's under PER_STAGE sharing — four stages (bids,
    // count, max, sink), eight slices at parallelism 2 — so the cluster's 12 slots and the
    // agent's state encoding carry over unchanged.
    static final Duration Q5_WINDOW = Duration.ofSeconds(10);
    static final Duration Q5_SLIDE = Duration.ofSeconds(5);

    private static void runQ5(StreamExecutionEnvironment env, int rate, int durationSec,
                              int heavyPar, long maxEventAgeMs) {
        // Q5 consumes only bids, so the whole rate is bids.
        final DataStream<Bid> bids = g(env
                .addSource(new RefBidSource(rate, durationSec, maxEventAgeMs))
                .name("bid-source").uid("bid-source"), "bid", "bid-source")
                .assignTimestampsAndWatermarks(
                        WatermarkStrategy.<Bid>forBoundedOutOfOrderness(Duration.ofSeconds(2))
                                .withTimestampAssigner((b, ts) -> b.dateTime.getMillis()))
                .name("bid-watermarks").uid("bid-watermarks");

        // Incremental COUNT per auction and window — the canonical formulation: one small
        // accumulator per (auction, window) in RocksDB rather than every bid buffered.
        final SingleOutputStreamOperator<Tuple3<Long, Long, Long>> counts = bids
                .keyBy((KeySelector<Bid, Long>) b -> b.auction)
                .window(SlidingEventTimeWindows.of(Q5_WINDOW, Q5_SLIDE))
                .aggregate(new CountBids(), new WithWindowEnd(),
                        TypeInformation.of(Long.class), TypeInformation.of(Long.class),
                        TypeInformation.of(new TypeHint<Tuple3<Long, Long, Long>>() {}));
        g(counts.setParallelism(heavyPar).name("hot-items-count").uid("hot-items-count"),
                "count", "hot-items-count");

        // The auction with the most bids per window, keyed by window end so it stays parallel.
        final SingleOutputStreamOperator<Tuple3<Long, Long, Long>> hottest = counts
                .keyBy((KeySelector<Tuple3<Long, Long, Long>, Long>) t -> t.f2)
                .process(new MaxPerWindow())
                .returns(TypeInformation.of(new TypeHint<Tuple3<Long, Long, Long>>() {}));
        g(hottest.name("hot-items-max").uid("hot-items-max"), "max", "hot-items-max");

        final DataStreamSink<Tuple3<Long, Long, Long>> sink = hottest
                .addSink(new HotItemsSink()).name("q5-sink").uid("q5-sink");
        if (slotSharing != GraphConfig.SlotSharingMode.SHARED) {
            sink.slotSharingGroup("snk");
        }
    }

    /** Bids per (auction, window), one long per key: Q5's incremental aggregate. */
    public static class CountBids implements AggregateFunction<Bid, Long, Long> {
        private static final long serialVersionUID = 1L;
        @Override public Long createAccumulator() { return 0L; }
        @Override public Long add(Bid b, Long acc) { return acc + 1; }
        @Override public Long getResult(Long acc) { return acc; }
        @Override public Long merge(Long a, Long b) { return a + b; }
    }

    /** Attaches the window end, the key of the next stage: (auction, count, windowEnd). */
    public static class WithWindowEnd
            extends ProcessWindowFunction<Long, Tuple3<Long, Long, Long>, Long, TimeWindow> {
        private static final long serialVersionUID = 1L;
        @Override
        public void process(Long auction, Context ctx, Iterable<Long> counts,
                            Collector<Tuple3<Long, Long, Long>> out) {
            out.collect(Tuple3.of(auction, counts.iterator().next(), ctx.window().getEnd()));
        }
    }

    /**
     * Keeps the highest count seen for one window end and emits it when the watermark passes
     * that end — by then every count for the window has arrived, since they are all emitted
     * when the same window fires upstream.
     */
    public static class MaxPerWindow extends KeyedProcessFunction<
            Long, Tuple3<Long, Long, Long>, Tuple3<Long, Long, Long>> {
        private static final long serialVersionUID = 1L;
        private transient ValueState<Tuple3<Long, Long, Long>> best;

        @Override
        public void open(org.apache.flink.api.common.functions.OpenContext ctx) {
            best = getRuntimeContext().getState(new ValueStateDescriptor<>(
                    "best", TypeInformation.of(new TypeHint<Tuple3<Long, Long, Long>>() {})));
        }

        @Override
        public void processElement(Tuple3<Long, Long, Long> v, Context ctx,
                                   Collector<Tuple3<Long, Long, Long>> out) throws Exception {
            final Tuple3<Long, Long, Long> current = best.value();
            if (current == null) {
                ctx.timerService().registerEventTimeTimer(v.f2);
            }
            if (current == null || v.f1 > current.f1) {
                best.update(v);
            }
        }

        @Override
        public void onTimer(long ts, OnTimerContext ctx,
                            Collector<Tuple3<Long, Long, Long>> out) throws Exception {
            final Tuple3<Long, Long, Long> winner = best.value();
            if (winner != null) {
                out.collect(winner);
            }
            best.clear();
        }
    }

    /** Reports the hottest auction per window, so a Q5 that emits nothing is visible. */
    public static class HotItemsSink extends RichSinkFunction<Tuple3<Long, Long, Long>> {
        private static final long serialVersionUID = 1L;
        private long received = 0;
        private long lastLog = 0;

        @Override
        public void invoke(Tuple3<Long, Long, Long> v, Context ctx) {
            received++;
            final long now = System.currentTimeMillis();
            if (now - lastLog > 5000) {
                lastLog = now;
                System.out.printf("[Q5-Sink-%d] windows=%,d last=(auction=%d,bids=%d,end=%d)%n",
                        getRuntimeContext().getTaskInfo().getIndexOfThisSubtask() + 1,
                        received, v.f0, v.f1, v.f2);
            }
        }
    }

    /**
     * Counts matches and reports them, because a Q8 that emits nothing looks exactly
     * like a Q8 that works. The project's own generator drew person ids and auction
     * sellers independently at random, so the join produced zero rows in every
     * campaign; this sink is the check that the reference sources fixed it.
     */
    public static class NewUsersSink extends RichSinkFunction<Tuple3<Long, String, Long>> {
        private static final long serialVersionUID = 1L;
        private long received = 0;
        private long lastLog = 0;

        @Override
        public void invoke(Tuple3<Long, String, Long> v, Context ctx) {
            received++;
            final long now = System.currentTimeMillis();
            if (now - lastLog > 5000) {
                lastLog = now;
                System.out.printf("[Q8-Sink-%d] matches=%,d last=(person=%d,name=%s,reserve=%d)%n",
                        getRuntimeContext().getTaskInfo().getIndexOfThisSubtask() + 1,
                        received, v.f0, v.f1, v.f2);
            }
        }
    }
}
