/*
 * Ported from ch.ethz.systems.strymon.ds2.flink.nexmark.sources.AuctionSourceFunction
 * (github.com/strymon-system/ds2), licensed to the Apache Software Foundation under
 * the Apache License, Version 2.0. See package-info.java.
 */
package com.thesis.benchmark.nexmark.ref;

import org.apache.beam.sdk.nexmark.NexmarkConfiguration;
import org.apache.beam.sdk.nexmark.model.Auction;
import org.apache.beam.sdk.nexmark.sources.generator.GeneratorConfig;
import org.apache.beam.sdk.nexmark.sources.generator.model.AuctionGenerator;
import org.apache.flink.metrics.Gauge;
import org.apache.flink.streaming.api.functions.source.legacy.RichParallelSourceFunction;

import java.util.Random;

/**
 * Emits Nexmark Auction events from Beam's reference generator.
 *
 * <p>The seller field is what makes this worth porting: Beam picks it as
 * {@code lastBase0PersonId(eventId)} rounded into a hot-seller bucket, or as
 * {@code nextBase0PersonId(...)}, always offset by {@code FIRST_PERSON_ID}. Every
 * auction therefore references a person the person stream really emits, which is
 * the property Q8's join needs and the project's own generator never had.
 */
public class RefAuctionSource extends RichParallelSourceFunction<Auction> {

    private static final long serialVersionUID = 1L;

    private final int totalRate;
    private final int durationSec;
    /**
     * How far behind schedule this source may fall before it SKIPS events instead of
     * emitting them late. 0 keeps the old behaviour of never skipping.
     *
     * <p>Nexmark derives an event's timestamp from its event NUMBER, so a source held
     * back by backpressure consumes fewer numbers and its event time advances more
     * slowly than the wall clock. Q8's 10-second windows then take far longer than
     * ten seconds to close and state accumulates across every window that never
     * fired: one window at 6500 ev/s is about 13 MB, and checkpoints on this cluster
     * reached 333 MB. Writing that to shared storage starved etcd — apply requests of
     * 600-1000 ms against a 100 ms budget — until the kubelet killed the API server,
     * seventy-four times.
     *
     * <p>Skipping turns unbounded queueing into bounded loss: the counter jumps to
     * where the schedule says it should be, event time tracks the wall clock, windows
     * fire on time and state stays within one window. It also sharpens the metric —
     * the source emits what the cluster can take and drops the rest, so the measured
     * rate is the capacity of the placement under test.
     */
    private final long maxEventAgeMs;
    private volatile boolean running = true;
    private transient GeneratorConfig config;
    private transient long eventsCountSoFar;
    /**
     * Milliseconds of the last one-second batch spent generating, i.e. this source's busy
     * time — the figure Flink cannot produce for it.
     *
     * <p>WHY (2026-09-15). {@code busyTimeMsPerSecond} is measured on the task mailbox, and a
     * legacy {@code SourceFunction} runs in its own thread outside it, so Flink reports NaN
     * for this vertex. {@code publish-loads.sh} turned that NaN into 0.0, the placement arms
     * read the Beam generator — the most CPU-hungry part of the whole job — as free, and LPT
     * put both person sources and both sinks on the one-core machine. With identical speeds,
     * memory and cluster, publishing that vector took LPT from +11.2% over STOCK to -11.0%.
     *
     * <p>The emission loop already times each batch before sleeping off the rest of the
     * second, so the measurement is free. It includes time blocked in {@code collect} under
     * backpressure, which Flink's own figure excludes; loads are sampled at a rate the
     * cluster sustains, where that term is close to zero.
     */
    private transient volatile long lastGeneratorBusyMs;
    private transient long epochsPerSecond;
    private transient int subtasks;
    private transient int index;

    public RefAuctionSource(int totalRate, int durationSec, long maxEventAgeMs) {
        this.totalRate = totalRate;
        this.durationSec = durationSec;
        this.maxEventAgeMs = maxEventAgeMs;
    }

    @Override
    public void open(org.apache.flink.api.common.functions.OpenContext ctx) {
        this.subtasks = getRuntimeContext().getTaskInfo().getNumberOfParallelSubtasks();
        this.index = getRuntimeContext().getTaskInfo().getIndexOfThisSubtask();
        // One sequence for every source, positioned by the wall clock (see NexmarkEpochs): the
        // config only supplies Beam's proportions and generator defaults, so its base time and
        // first id are irrelevant and left at zero. Replaces a per-source counter that made
        // persons repeat ~50 times and auctions outrun persons until the join matched nothing.
        this.config = new GeneratorConfig(NexmarkConfiguration.DEFAULT, 0L, 0L, 0L, 0L);
        this.epochsPerSecond = Math.max(1L, (long) totalRate / Math.max(1, GeneratorConfig.AUCTION_PROPORTION));
        this.eventsCountSoFar = 0;
        getRuntimeContext()
                .getMetricGroup()
                .gauge("generatorBusyMsPerSecond", (Gauge<Long>) () -> lastGeneratorBusyMs);
    }

    @Override
    public void run(SourceContext<Auction> ctx) throws Exception {
        final long startedAt = System.currentTimeMillis();
        final long deadline = durationSec > 0 ? startedAt + durationSec * 1000L : Long.MAX_VALUE;
        final long total = GeneratorConfig.PROPORTION_DENOMINATOR;
        final int persons = GeneratorConfig.PERSON_PROPORTION;
        final int auctions = GeneratorConfig.AUCTION_PROPORTION;
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
                for (int slot = 0; slot < auctions; slot++) {
                    // Only the auction slots: numbers persons .. persons+auctions-1, so the
                    // seller each one names is a person the other source emits in the same epoch
                    // range — within milliseconds, i.e. inside the same window.
                    final long eventId = total * nextEpoch + persons + slot;
                    // nextAuction takes the raw millisecond timestamp, unlike nextPerson,
                    // which takes a joda DateTime. That asymmetry is Beam's, not a typo.
                    ctx.collect(AuctionGenerator.nextAuction(
                            eventsCountSoFar, eventId, new Random(eventId), timestamp, config));
                    eventsCountSoFar++;
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
