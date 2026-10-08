/*
 * Ported from ch.ethz.systems.strymon.ds2.flink.nexmark.sources.PersonSourceFunction
 * (github.com/strymon-system/ds2), licensed to the Apache Software Foundation under
 * the Apache License, Version 2.0. See package-info.java for provenance and for the
 * deviations this port makes.
 */
package com.thesis.benchmark.nexmark.ref;

import org.apache.beam.sdk.nexmark.NexmarkConfiguration;
import org.apache.beam.sdk.nexmark.model.Person;
import org.apache.beam.sdk.nexmark.sources.generator.GeneratorConfig;
import org.apache.beam.sdk.nexmark.sources.generator.model.PersonGenerator;
import org.apache.flink.metrics.Gauge;
import org.apache.flink.streaming.api.functions.source.legacy.RichParallelSourceFunction;
import org.joda.time.DateTime;

import java.util.Random;

/** Emits Nexmark Person events from Beam's reference generator. */
public class RefPersonSource extends RichParallelSourceFunction<Person> {

    private static final long serialVersionUID = 1L;

    /** Events per second for the WHOLE source, split across its subtasks. */
    private final int totalRate;
    /** Seconds to run before stopping, or 0 to run until cancelled. The harness
     *  submits jobs with a duration and expects them to end on their own; a source
     *  that never stops leaves the cluster occupied for the next campaign cell. */
    private final int durationSec;
    /**
     * How far behind schedule this source may fall before it SKIPS events instead
     * of emitting them late. 0 keeps the old behaviour of never skipping.
     *
     * <p>WHY THIS EXISTS (2026-09-03). Nexmark derives an event's timestamp from its
     * event NUMBER, so a source held back by backpressure consumes fewer numbers and
     * its event time advances more slowly than the wall clock. Q8's 10-second
     * tumbling windows then take much longer than ten seconds to close, and state
     * accumulates across all the windows that never fired. Measured on this cluster:
     * one window at 6500 ev/s is about 13 MB, and checkpoints reached 333 MB —
     * twenty-five windows still open. That state is written to shared storage, which
     * starved etcd (apply requests of 600-1000 ms against a 100 ms budget) until the
     * kubelet killed the API server, seventy-four times.
     *
     * <p>Skipping converts unbounded queueing into bounded loss: the source jumps its
     * counter forward to where the schedule says it should be, so event time tracks
     * the wall clock, windows fire on time and state stays within one window. It also
     * makes throughput mean what we want it to mean — the source emits what the
     * cluster can take and drops the rest, so the measured rate IS the capacity of
     * the placement under test.
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

    public RefPersonSource(int totalRate, int durationSec, long maxEventAgeMs) {
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
        this.epochsPerSecond = Math.max(1L, (long) totalRate / Math.max(1, GeneratorConfig.PERSON_PROPORTION));
        this.eventsCountSoFar = 0;
        getRuntimeContext()
                .getMetricGroup()
                .gauge("generatorBusyMsPerSecond", (Gauge<Long>) () -> lastGeneratorBusyMs);
    }

    @Override
    public void run(SourceContext<Person> ctx) throws Exception {
        // Monotonic since 2026-10-04, see NexmarkEpochs.Clock: WSL2 steps its wall clock, and
        // the same defect cost Q5's bid source up to 29% of its events.
        final NexmarkEpochs.Clock clock = new NexmarkEpochs.Clock();
        final long startedAt = clock.nowMillis();
        long secondStart = startedAt;
        long busyThisSecond = 0;
        final long deadline = durationSec > 0 ? startedAt + durationSec * 1000L : Long.MAX_VALUE;
        final long total = GeneratorConfig.PROPORTION_DENOMINATOR;
        final int persons = GeneratorConfig.PERSON_PROPORTION;
        long nextEpoch = NexmarkEpochs.alignUp(
                NexmarkEpochs.epochAt(startedAt, epochsPerSecond), subtasks, index);
        while (running && clock.nowMillis() < deadline) {
            final long emitStart = clock.nowMillis();
            final long due = NexmarkEpochs.epochAt(emitStart, epochsPerSecond);
            // Behind by more than the allowed age: jump to where the clock is. Both sources
            // compute the same epoch from the same clock, so this cannot desynchronise them.
            if (maxEventAgeMs > 0) {
                final long oldest = NexmarkEpochs.epochAt(emitStart - maxEventAgeMs, epochsPerSecond);
                if (nextEpoch < oldest) {
                    nextEpoch = NexmarkEpochs.alignUp(oldest, subtasks, index);
                }
            }
            while (running && nextEpoch <= due) {
                final long timestamp = NexmarkEpochs.timeOf(nextEpoch, epochsPerSecond);
                for (int slot = 0; slot < persons; slot++) {
                    // Only the person slots of the epoch: numbers 0 .. persons-1.
                    final long eventId = total * nextEpoch + slot;
                    ctx.collect(PersonGenerator.nextPerson(
                            eventId, new Random(eventId), new DateTime(timestamp), config));
                    eventsCountSoFar++;
                }
                nextEpoch += subtasks;
            }
            final long elapsed = clock.nowMillis() - emitStart;
            // Busy time stays a per-SECOND figure, summed over the passes of the last second.
            busyThisSecond += elapsed;
            if (emitStart - secondStart >= 1000L) {
                lastGeneratorBusyMs = Math.min(busyThisSecond, 1000L);
                busyThisSecond = 0;
                secondStart = emitStart;
            }
            if (elapsed < NexmarkEpochs.EMIT_PERIOD_MS) {
                Thread.sleep(NexmarkEpochs.EMIT_PERIOD_MS - elapsed);
            }
        }
    }

    @Override
    public void cancel() {
        running = false;
    }
}
