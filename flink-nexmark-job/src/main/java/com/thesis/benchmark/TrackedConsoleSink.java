package com.thesis.benchmark;

import org.apache.flink.api.connector.sink2.Sink;
import org.apache.flink.api.connector.sink2.SinkWriter;
import org.apache.flink.api.connector.sink2.WriterInitContext;
import org.apache.flink.api.java.tuple.Tuple2;

/**
 * The end-to-end latency sink as a Sink V2, replacing the {@code RichSinkFunction} that Flink 2.0
 * removed.
 *
 * <p>It writes nothing anywhere: its whole purpose is to close the pipeline and record how long
 * each record took from the timestamp the generator stamped on it to the moment it arrived here.
 * Those percentiles are what the campaign reports as end-to-end delay, and that measurement turned
 * out to be more sensitive than throughput at separating placement policies.
 *
 * <p>The periodic log line is kept because it is the only in-job signal that the pipeline is alive
 * at the expected rate while a run is in flight; the harness reads Flink's own metrics rather than
 * this output.
 */
public class TrackedConsoleSink implements Sink<Tuple2<Long, Double>> {

    private static final long serialVersionUID = 1L;

    @Override
    public SinkWriter<Tuple2<Long, Double>> createWriter(WriterInitContext context) {
        return new Writer(subtaskIndexOf(context));
    }

    /**
     * Sink V2's init context exposes no subtask id directly; the metric group carries it as a
     * variable. Used for the log prefix only, so an unexpected shape degrades to -1 rather than
     * failing a run.
     */
    private static int subtaskIndexOf(WriterInitContext context) {
        try {
            final String raw = context.metricGroup().getAllVariables().get("<subtask_index>");
            return raw == null ? -1 : Integer.parseInt(raw);
        } catch (RuntimeException e) {
            return -1;
        }
    }

    private static class Writer implements SinkWriter<Tuple2<Long, Double>> {

        private final int subtaskIndex;
        private final ConfigurableGraphJob.LatencyHistogram histogram =
                new ConfigurableGraphJob.LatencyHistogram();

        private long recordCount;
        private long firstRecordTime;
        private long lastRecordTime;
        private long lastLogTime = System.currentTimeMillis();
        private long lastLogCount;

        Writer(int subtaskIndex) {
            this.subtaskIndex = subtaskIndex;
        }

        @Override
        public void write(Tuple2<Long, Double> element, Context context) {
            final long now = System.currentTimeMillis();
            recordCount++;
            if (recordCount == 1) {
                firstRecordTime = now;
            }
            lastRecordTime = now;

            final Long eventTimestamp = context.timestamp();
            if (eventTimestamp != null && eventTimestamp > 0) {
                final long latency = now - eventTimestamp;
                if (latency >= 0) {
                    histogram.add(latency);
                }
            }

            // Logged from the writing thread rather than a daemon thread: Sink V2 gives no lifecycle
            // hook to stop one, and a leaked thread outlives the rescale that killed its writer.
            if (now - lastLogTime >= 10_000) {
                final double rate = (recordCount - lastLogCount) / ((now - lastLogTime) / 1000.0);
                System.out.printf(
                        "[Sink-%d] records=%d rate=%.0f/s%n", subtaskIndex, recordCount, rate);
                lastLogTime = now;
                lastLogCount = recordCount;
            }
        }

        @Override
        public void flush(boolean endOfInput) {
            // Nothing is buffered downstream; the histogram is reported on close.
        }

        @Override
        public void close() {
            System.out.printf(
                    "[Sink-%d] FINAL records=%d firstRecord=%d lastRecord=%d%n",
                    subtaskIndex, recordCount, firstRecordTime, lastRecordTime);
            final ConfigurableGraphJob.LatencyStats stats = histogram.snapshot();
            if (stats.count > 0) {
                System.out.printf(
                        "[Latency-TOTAL-%d] FINAL count=%d min=%dms p50=%dms p95=%dms p99=%dms max=%dms avg=%.1fms%n",
                        subtaskIndex,
                        stats.count,
                        stats.min,
                        stats.p50,
                        stats.p95,
                        stats.p99,
                        stats.max,
                        stats.avg);
            }
        }
    }
}
