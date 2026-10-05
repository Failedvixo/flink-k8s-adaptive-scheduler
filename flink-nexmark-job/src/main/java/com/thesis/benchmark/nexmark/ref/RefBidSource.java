/*
 * Modelled on RefAuctionSource, itself ported from DS2's AuctionSourceFunction
 * (github.com/strymon-system/ds2, Apache License 2.0). See package-info.java.
 */
package com.thesis.benchmark.nexmark.ref;

import org.apache.beam.sdk.nexmark.NexmarkConfiguration;
import org.apache.beam.sdk.nexmark.model.Bid;
import org.apache.beam.sdk.nexmark.sources.generator.GeneratorConfig;
import org.apache.beam.sdk.nexmark.sources.generator.model.BidGenerator;
import org.apache.flink.metrics.Counter;
import org.apache.flink.metrics.Gauge;
import org.apache.flink.streaming.api.functions.source.legacy.RichParallelSourceFunction;

import java.util.Random;

/**
 * Emits Nexmark Bid events from Beam's reference generator, for Q5.
 *
 * <p>Same construction as {@link RefAuctionSource}: the source walks the shared, wall-clock
 * anchored event sequence of {@link NexmarkEpochs} and emits only the numbers that belong to
 * its type — in every epoch of 50, the last 46 (after one person and three auctions). Beam
 * derives a bid's auction from the event number, so bids name auctions the way the reference
 * generator does, hot auctions included, without an auction source having to run.
 *
 * <p>Q5 is the second query of the thesis for a specific reason: it holds keyed window state
 * like Q8's join, but through a different operator — a sliding-window aggregation with one
 * input — so a placement policy learned on Q8 can be tested on an operator it has never seen.
 */
public class RefBidSource extends RichParallelSourceFunction<Bid> {

    private static final long serialVersionUID = 1L;

    private final int totalRate;
    private final int durationSec;
    /** See {@link RefAuctionSource}: how far behind schedule before events are skipped. */
    private final long maxEventAgeMs;
    private volatile boolean running = true;
    private transient GeneratorConfig config;
    /** This source's own busy time; Flink reports NaN for a legacy source (see RefAuctionSource). */
    private transient volatile long lastGeneratorBusyMs;
    private transient long epochsPerSecond;
    private transient int subtasks;
    private transient int index;
    /**
     * DIAGNOSTICS (2026-10-04). In the cluster this source emitted ~90% of what it was asked
     * for — and as little as 71% — with its generator busy 3 ms per second and every operator
     * downstream idle, while the same loop delivers 100% when simulated offline and Q8's sources
     * deliver 100% on the same cluster. These three say where the difference is: our own count
     * of what was handed to collect() (against Flink's numRecordsOut), how far behind the
     * wall-clock sequence the loop starts each pass, and the longest gap between passes.
     */
    private transient Counter emitted;
    private transient volatile long lastLagEpochs;
    private transient volatile long maxGapMs;
    private transient volatile long skippedEpochs;

    public RefBidSource(int totalRate, int durationSec, long maxEventAgeMs) {
        this.totalRate = totalRate;
        this.durationSec = durationSec;
        this.maxEventAgeMs = maxEventAgeMs;
    }

    /** Bids per epoch: what is left of Beam's 50 after one person and three auctions. */
    static int bidsPerEpoch() {
        return GeneratorConfig.PROPORTION_DENOMINATOR
                - GeneratorConfig.PERSON_PROPORTION - GeneratorConfig.AUCTION_PROPORTION;
    }

    @Override
    public void open(org.apache.flink.api.common.functions.OpenContext ctx) {
        this.subtasks = getRuntimeContext().getTaskInfo().getNumberOfParallelSubtasks();
        this.index = getRuntimeContext().getTaskInfo().getIndexOfThisSubtask();
        this.config = new GeneratorConfig(NexmarkConfiguration.DEFAULT, 0L, 0L, 0L, 0L);
        this.epochsPerSecond = Math.max(1L, (long) totalRate / Math.max(1, bidsPerEpoch()));
        final org.apache.flink.metrics.MetricGroup group = getRuntimeContext().getMetricGroup();
        group.gauge("generatorBusyMsPerSecond", (Gauge<Long>) () -> lastGeneratorBusyMs);
        this.emitted = group.counter("bidsHandedToCollect");
        group.gauge("loopLagEpochs", (Gauge<Long>) () -> lastLagEpochs);
        group.gauge("loopMaxGapMs", (Gauge<Long>) () -> maxGapMs);
        group.gauge("skippedEpochs", (Gauge<Long>) () -> skippedEpochs);
    }

    @Override
    public void run(SourceContext<Bid> ctx) throws Exception {
        // Monotonic, see NexmarkEpochs.Clock: the wall clock steps on WSL2 and cost this source
        // up to 29% of its events.
        final NexmarkEpochs.Clock clock = new NexmarkEpochs.Clock();
        final long startedAt = clock.nowMillis();
        final long deadline = durationSec > 0 ? startedAt + durationSec * 1000L : Long.MAX_VALUE;
        final long total = GeneratorConfig.PROPORTION_DENOMINATOR;
        final int firstBid = GeneratorConfig.PERSON_PROPORTION + GeneratorConfig.AUCTION_PROPORTION;
        final int bids = bidsPerEpoch();
        long nextEpoch = NexmarkEpochs.alignUp(
                NexmarkEpochs.epochAt(startedAt, epochsPerSecond), subtasks, index);
        long previousStart = -1;
        while (running && clock.nowMillis() < deadline) {
            final long emitStart = clock.nowMillis();
            if (previousStart > 0) {
                maxGapMs = Math.max(maxGapMs, emitStart - previousStart);
            }
            previousStart = emitStart;
            final long due = NexmarkEpochs.epochAt(emitStart, epochsPerSecond);
            lastLagEpochs = due - nextEpoch;
            if (maxEventAgeMs > 0) {
                final long oldest = NexmarkEpochs.epochAt(emitStart - maxEventAgeMs, epochsPerSecond);
                if (nextEpoch < oldest) {
                    final long jumped = NexmarkEpochs.alignUp(oldest, subtasks, index);
                    skippedEpochs += (jumped - nextEpoch) / subtasks;
                    nextEpoch = jumped;
                }
            }
            while (running && nextEpoch <= due) {
                final long timestamp = NexmarkEpochs.timeOf(nextEpoch, epochsPerSecond);
                for (int slot = 0; slot < bids; slot++) {
                    final long eventId = total * nextEpoch + firstBid + slot;
                    // nextBid takes the raw millisecond timestamp, like nextAuction.
                    ctx.collect(BidGenerator.nextBid(
                            eventId, new Random(eventId), timestamp, config));
                }
                emitted.inc(bids);
                nextEpoch += subtasks;
            }
            final long elapsed = clock.nowMillis() - emitStart;
            lastGeneratorBusyMs = Math.min(elapsed, 1000L);
            if (elapsed < 1000) {
                Thread.sleep(1000 - elapsed);
            }
        }
    }

    @Override
    public void cancel() {
        running = false;
    }
}
