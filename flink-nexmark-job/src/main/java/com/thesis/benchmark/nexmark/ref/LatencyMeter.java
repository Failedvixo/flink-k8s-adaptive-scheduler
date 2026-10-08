package com.thesis.benchmark.nexmark.ref;

import org.apache.flink.metrics.Gauge;
import org.apache.flink.metrics.MetricGroup;

import java.util.Arrays;

/**
 * End-to-end latency of each result, as the thesis defines it: the time from when its input
 * entered the graph to when the result leaves it.
 *
 * <p>WHY (2026-10-08). The harness used to report "e2e" as wall clock minus the sink's input
 * watermark. That is how far the sink trails in EVENT time, not how long a record took, and it
 * does not exist at all for Q3, whose join has no watermarks. Here every sink records, for each
 * result, {@code now - inputTime}, where inputTime is the generation time the sources stamped on
 * the events that produced it (the epoch time of {@link NexmarkEpochs}: when the event was due,
 * so time spent queued at a backpressured source counts, as it should).
 *
 * <p>What "input time" is per query: Q8 and Q3 — the LATER of the person and the auction, the
 * moment the pair could first exist; Q5 — the end of the window, the moment the count could
 * first be final. So Q8 and Q5 include the wait for their windows by construction, and Q3,
 * which has no window, does not.
 *
 * <p>Both clocks are {@link NexmarkEpochs.Clock}: anchored to the wall clock once and advanced by
 * the monotonic clock, so a WSL2 clock step does not show up as latency. Every task of a job
 * opens within a second of the others, so the anchors agree.
 *
 * <p>Exposed as gauges over the results of the last {@link #WINDOW_MS} (at most {@link #CAPACITY}
 * of them): p50, p99, mean, and how many results that is. A time window rather than "the last N"
 * because the result rate differs by three orders of magnitude between queries — Q8 emits ~2000
 * results/s, Q5 one per window — and a count would reach back into the warm-up for Q5. A gauge is
 * read every few seconds by the harness, so filtering and sorting a copy on read is cheap.
 */
final class LatencyMeter {

    static final int CAPACITY = 50_000;
    static final long WINDOW_MS = 60_000L;

    private final long[] ring = new long[CAPACITY];
    private final long[] at = new long[CAPACITY];
    private int next = 0;
    private int size = 0;
    private final NexmarkEpochs.Clock clock = new NexmarkEpochs.Clock();

    LatencyMeter(MetricGroup group) {
        group.gauge("latencyP50Ms", (Gauge<Long>) () -> percentile(0.50));
        group.gauge("latencyP99Ms", (Gauge<Long>) () -> percentile(0.99));
        group.gauge("latencyMeanMs", (Gauge<Long>) this::mean);
        group.gauge("latencySamples", (Gauge<Integer>) () -> snapshot().length);
    }

    /** Record one result whose inputs entered the graph at {@code inputTimeMs}. */
    synchronized void record(long inputTimeMs) {
        final long now = clock.nowMillis();
        ring[next] = Math.max(0L, now - inputTimeMs);
        at[next] = now;
        next = (next + 1) % CAPACITY;
        if (size < CAPACITY) {
            size++;
        }
    }

    private synchronized long[] snapshot() {
        final long cutoff = clock.nowMillis() - WINDOW_MS;
        final long[] out = new long[size];
        int n = 0;
        for (int i = 0; i < size; i++) {
            if (at[i] >= cutoff) {
                out[n++] = ring[i];
            }
        }
        return Arrays.copyOf(out, n);
    }

    private long percentile(double q) {
        final long[] values = snapshot();
        if (values.length == 0) {
            return -1L;
        }
        Arrays.sort(values);
        return values[Math.min(values.length - 1, (int) Math.floor(q * values.length))];
    }

    private long mean() {
        final long[] values = snapshot();
        if (values.length == 0) {
            return -1L;
        }
        long sum = 0;
        for (long v : values) {
            sum += v;
        }
        return sum / values.length;
    }
}
