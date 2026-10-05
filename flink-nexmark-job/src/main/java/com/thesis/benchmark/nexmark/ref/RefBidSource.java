/*
 * Modelled on RefAuctionSource, itself ported from DS2's AuctionSourceFunction
 * (github.com/strymon-system/ds2, Apache License 2.0). See package-info.java.
 */
package com.thesis.benchmark.nexmark.ref;

import org.apache.beam.sdk.nexmark.NexmarkConfiguration;
import org.apache.beam.sdk.nexmark.model.Bid;
import org.apache.beam.sdk.nexmark.sources.generator.GeneratorConfig;
import org.apache.beam.sdk.nexmark.sources.generator.model.BidGenerator;
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
        getRuntimeContext()
                .getMetricGroup()
                .gauge("generatorBusyMsPerSecond", (Gauge<Long>) () -> lastGeneratorBusyMs);
    }

    @Override
    public void run(SourceContext<Bid> ctx) throws Exception {
        final long startedAt = System.currentTimeMillis();
        final long deadline = durationSec > 0 ? startedAt + durationSec * 1000L : Long.MAX_VALUE;
        final long total = GeneratorConfig.PROPORTION_DENOMINATOR;
        final int firstBid = GeneratorConfig.PERSON_PROPORTION + GeneratorConfig.AUCTION_PROPORTION;
        final int bids = bidsPerEpoch();
        long nextEpoch = NexmarkEpochs.alignUp(
                NexmarkEpochs.epochAt(startedAt, epochsPerSecond), subtasks, index);
        while (running && System.currentTimeMillis() < deadline) {
            final long emitStart = System.currentTimeMillis();
            final long due = NexmarkEpochs.epochAt(emitStart, epochsPerSecond);
            if (maxEventAgeMs > 0) {
                final long oldest = NexmarkEpochs.epochAt(emitStart - maxEventAgeMs, epochsPerSecond);
                if (nextEpoch < oldest) {
                    nextEpoch = NexmarkEpochs.alignUp(oldest, subtasks, index);
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
                nextEpoch += subtasks;
            }
            final long elapsed = System.currentTimeMillis() - emitStart;
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
