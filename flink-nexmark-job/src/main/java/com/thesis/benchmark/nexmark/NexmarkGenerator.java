package com.thesis.benchmark.nexmark;

import org.apache.flink.streaming.api.functions.source.legacy.RichParallelSourceFunction;

import java.util.Random;
import java.util.concurrent.ArrayBlockingQueue;
import java.util.concurrent.BlockingQueue;
import java.util.concurrent.ThreadLocalRandom;
import java.util.concurrent.atomic.AtomicLong;

/**
 * Single-source Nexmark event generator.
 *
 * Emits a tagged stream of Person/Auction/Bid events in the canonical
 * Nexmark proportions (1 : 3 : 46) so that downstream queries see a
 * realistic interleaving. Mirrors the rate-shaping (CONSTANT / SINE /
 * STEP) and stale-drop policy used by ConfigurableGraphJob so results
 * are comparable to the existing benchmark family.
 *
 * Auction IDs are sampled with Zipf-like skew (parameter {@code zipfAlpha})
 * so that Q5 (Hot Items) actually has hot items.
 */
public class NexmarkGenerator extends RichParallelSourceFunction<NexmarkEvent> {

    // Canonical Nexmark proportions (Beam reference).
    private static final int P_W = 1;
    private static final int A_W = 3;
    private static final int B_W = 46;
    private static final int TOTAL_W = P_W + A_W + B_W;

    /** Where the RAMP distribution starts, as a fraction of the base rate. */
    private static final double RAMP_START_FRAC = 0.25;

    private final long baseRate;          // events/sec across all 3 streams
    private final int durationSec;
    private final String distribution;    // CONSTANT | SINE | STEP | RAMP
    private final double sineAmplitude;
    private final double stepHighFrac;    // multiplier on baseRate during the high phase
    private final double zipfAlpha;       // 0 = uniform; 1.0 = strong skew
    private final long maxEventAgeMs;     // stale-drop threshold; <=0 disables
    private final int hotAuctionPool;     // size of "active" auction pool keyed by Zipf

    private volatile boolean running = true;

    public NexmarkGenerator(long baseRate,
                            int durationSec,
                            String distribution,
                            double sineAmplitude,
                            double stepHighFrac,
                            double zipfAlpha,
                            long maxEventAgeMs,
                            int hotAuctionPool) {
        this.baseRate = baseRate;
        this.durationSec = durationSec;
        this.distribution = distribution.toUpperCase();
        this.sineAmplitude = sineAmplitude;
        this.stepHighFrac = stepHighFrac;
        this.zipfAlpha = zipfAlpha;
        this.maxEventAgeMs = maxEventAgeMs;
        this.hotAuctionPool = Math.max(1, hotAuctionPool);
    }

    @Override
    public void run(SourceContext<NexmarkEvent> ctx) throws Exception {
        int subtask = getRuntimeContext().getTaskInfo().getIndexOfThisSubtask();
        int parallelism = getRuntimeContext().getTaskInfo().getNumberOfParallelSubtasks();

        long startMs = System.currentTimeMillis();
        long endMs = startMs + durationSec * 1000L;

        BlockingQueue<NexmarkEvent> queue = new ArrayBlockingQueue<>(50_000);
        AtomicLong generated = new AtomicLong();
        AtomicLong emitted   = new AtomicLong();
        AtomicLong dropped   = new AtomicLong();

        // Per-subtask rate (split across parallel sources).
        long subBaseRate = Math.max(1L, baseRate / parallelism);

        System.out.printf(
                "[NexmarkGen-%d/%d] dist=%s base=%d peakFrac=%.2f zipf=%.2f maxAge=%dms pool=%d%n",
                subtask + 1, parallelism, distribution, subBaseRate,
                effectivePeakFraction(), zipfAlpha, maxEventAgeMs, hotAuctionPool);

        // -------- Producer --------
        Thread producer = new Thread(() -> {
            long pStart = System.currentTimeMillis();
            long batchStart = System.nanoTime();
            long emittedInBatch = 0;
            ThreadLocalRandom rng = ThreadLocalRandom.current();
            while (running && System.currentTimeMillis() < endMs) {
                long now = System.currentTimeMillis();
                double elapsedSec = (now - pStart) / 1000.0;
                long curRate = computeRate(subBaseRate, elapsedSec);
                if (curRate <= 0) {
                    sleepQuiet(50);
                    continue;
                }
                // 100ms batches
                long batchSize = Math.max(1, curRate / 10);

                for (long i = 0; i < batchSize && running; i++) {
                    NexmarkEvent e = nextEvent(rng, now);
                    generated.incrementAndGet();
                    if (!queue.offer(e)) {
                        dropped.incrementAndGet();   // queue full → drop at producer
                    }
                }
                emittedInBatch += batchSize;
                long nextBatchTime = batchStart + (emittedInBatch * 1_000_000_000L) / Math.max(1, curRate);
                long sleepNanos = nextBatchTime - System.nanoTime();
                if (sleepNanos > 0) {
                    try { Thread.sleep(sleepNanos / 1_000_000, (int) (sleepNanos % 1_000_000)); }
                    catch (InterruptedException ie) { return; }
                }
            }
        }, "nexmark-producer-" + subtask);
        producer.setDaemon(true);
        producer.start();

        // -------- Stats reporter --------
        Thread reporter = new Thread(() -> {
            long lastLog = System.currentTimeMillis();
            while (running && System.currentTimeMillis() < endMs + 2000) {
                sleepQuiet(10_000);
                long now = System.currentTimeMillis();
                double dt = (now - lastLog) / 1000.0;
                lastLog = now;
                System.out.printf(
                        "[NexmarkGen-%d] t=%.0fs gen=%,d (%.0f/s) emit=%,d (%.0f/s) drop=%,d (%.1f%%) q=%d%n",
                        subtask + 1, (now - startMs) / 1000.0,
                        generated.get(), generated.get() / Math.max(0.001, (now - startMs) / 1000.0),
                        emitted.get(), emitted.get() / Math.max(0.001, (now - startMs) / 1000.0),
                        dropped.get(),
                        generated.get() == 0 ? 0.0 : (dropped.get() * 100.0 / generated.get()),
                        queue.size());
            }
        }, "nexmark-reporter-" + subtask);
        reporter.setDaemon(true);
        reporter.start();

        // -------- Consumer / emit loop --------
        while (running && System.currentTimeMillis() < endMs + 1000) {
            NexmarkEvent e = queue.poll();
            if (e == null) {
                sleepQuiet(1);
                continue;
            }
            if (maxEventAgeMs > 0) {
                long age = System.currentTimeMillis() - e.eventTime;
                if (age > maxEventAgeMs) {
                    dropped.incrementAndGet();   // stale on emit
                    continue;
                }
            }
            ctx.collectWithTimestamp(e, e.eventTime);
            emitted.incrementAndGet();
        }

        running = false;
        long g = generated.get(), em = emitted.get(), dr = dropped.get();
        double elapsed = (System.currentTimeMillis() - startMs) / 1000.0;
        System.out.printf(
                "[NexmarkGen-%d] DONE in %.1fs: generated=%,d, emitted=%,d, dropped=%,d (%.1f%%)%n",
                subtask + 1, elapsed, g, em, dr,
                g == 0 ? 0.0 : (dr * 100.0 / g));
    }

    @Override
    public void cancel() { running = false; }

    // ---------- Event generation ----------

    private NexmarkEvent nextEvent(Random rng, long now) {
        int pick = rng.nextInt(TOTAL_W);
        if (pick < P_W) return NexmarkEvent.of(makePerson(rng, now));
        if (pick < P_W + A_W) return NexmarkEvent.of(makeAuction(rng, now));
        return NexmarkEvent.of(makeBid(rng, now));
    }

    private static final String[] CITIES = {"Phoenix","Seattle","Portland","Boston","Austin","Miami","Denver"};
    private static final String[] STATES = {"AZ","WA","OR","MA","TX","FL","CO"};
    private static final String[] CHANNELS = {"Apple","Google","Amazon","Facebook","Other"};
    private static final String[] ITEMS = {"book","watch","car","laptop","camera","bike","phone","guitar"};

    private Person makePerson(Random rng, long now) {
        long id = rng.nextLong() & 0x7FFFFFFFL;
        int ci = rng.nextInt(CITIES.length);
        return new Person(id, "p" + id,
                "p" + id + "@example.com", CITIES[ci], STATES[ci], now);
    }

    private Auction makeAuction(Random rng, long now) {
        long id = rng.nextLong() & 0x7FFFFFFFL;
        long seller = rng.nextLong() & 0x7FFFFFFFL;
        long initial = 1 + rng.nextInt(100);
        long reserve = initial + rng.nextInt(900);
        long expires = now + 30_000 + rng.nextInt(60_000);
        long category = rng.nextInt(10);
        return new Auction(id, ITEMS[rng.nextInt(ITEMS.length)] + "-" + id,
                initial, reserve, now, expires, seller, category);
    }

    private Bid makeBid(Random rng, long now) {
        long auctionId = zipfPick(rng);  // skewed → hot items
        long bidder = rng.nextLong() & 0x7FFFFFFFL;
        long price = 1 + rng.nextInt(1000);
        String channel = CHANNELS[rng.nextInt(CHANNELS.length)];
        return new Bid(auctionId, bidder, price, channel, now);
    }

    /**
     * Pick an auction id from [0, hotAuctionPool) with Zipf-like skew.
     * For zipfAlpha=0 → uniform. Higher α → heavier head.
     */
    private long zipfPick(Random rng) {
        if (zipfAlpha <= 0.0) return rng.nextInt(hotAuctionPool);
        // Inverse-CDF sampling on a truncated Zipf(α) over [1, N].
        double u = rng.nextDouble();
        double pow = Math.pow(u, 1.0 / (1.0 + zipfAlpha));
        long k = (long) (hotAuctionPool * pow);
        if (k < 0) k = 0;
        if (k >= hotAuctionPool) k = hotAuctionPool - 1;
        return k;
    }

    // ---------- Rate shaping ----------

    private long computeRate(long base, double elapsedSec) {
        switch (distribution) {
            case "SINE": {
                double phase = 2 * Math.PI * elapsedSec / 60.0;
                return Math.max(0, (long) (base * (1.0 + sineAmplitude * Math.sin(phase))));
            }
            case "STEP": {
                double frac = elapsedSec / Math.max(1.0, durationSec);
                if (frac < 1.0 / 3.0) return Math.max(1, (long) (base * 0.4));
                if (frac < 2.0 / 3.0) return (long) (base * stepHighFrac);
                return Math.max(1, (long) (base * 0.4));
            }
            case "RAMP": {
                // Linear climb from RAMP_START_FRAC*base to stepHighFrac*base over the run.
                // Unlike STEP, the rate never returns: the autoscaler faces a demand that only
                // grows, so every scaling decision it makes is one it has to live with.
                double frac = Math.min(1.0, elapsedSec / Math.max(1.0, durationSec));
                double multiplier = RAMP_START_FRAC + (stepHighFrac - RAMP_START_FRAC) * frac;
                return Math.max(1, (long) (base * multiplier));
            }
            case "CONSTANT":
            default:
                return base;
        }
    }

    private double effectivePeakFraction() {
        switch (distribution) {
            case "SINE": return 1.0 + sineAmplitude;
            case "STEP":
            case "RAMP": return stepHighFrac;   // RAMP ends exactly at the STEP high rate
            default:     return 1.0;
        }
    }

    private static void sleepQuiet(long ms) {
        try { Thread.sleep(ms); } catch (InterruptedException ignored) { }
    }
}
