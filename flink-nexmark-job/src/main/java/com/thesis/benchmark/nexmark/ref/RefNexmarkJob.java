package com.thesis.benchmark.nexmark.ref;

import com.thesis.benchmark.GraphConfig;
import org.apache.beam.sdk.nexmark.model.Auction;
import org.apache.beam.sdk.nexmark.model.Person;
import org.apache.flink.api.common.eventtime.WatermarkStrategy;
import org.apache.flink.api.common.functions.JoinFunction;
import org.apache.flink.api.java.functions.KeySelector;
import org.apache.flink.api.java.tuple.Tuple3;
import org.apache.flink.streaming.api.datastream.DataStream;
import org.apache.flink.streaming.api.datastream.DataStreamSink;
import org.apache.flink.streaming.api.datastream.SingleOutputStreamOperator;
import org.apache.flink.streaming.api.environment.StreamExecutionEnvironment;
import org.apache.flink.streaming.api.functions.sink.legacy.RichSinkFunction;
import org.apache.flink.streaming.api.windowing.assigners.TumblingEventTimeWindows;

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
        } else {
            throw new IllegalArgumentException(
                    "only q8 is ported to the reference sources so far, got " + query);
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
