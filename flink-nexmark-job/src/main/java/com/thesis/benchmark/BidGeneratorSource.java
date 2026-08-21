package com.thesis.benchmark;

import org.apache.flink.api.connector.source.Boundedness;
import org.apache.flink.api.connector.source.ReaderOutput;
import org.apache.flink.api.connector.source.Source;
import org.apache.flink.api.connector.source.SourceReader;
import org.apache.flink.api.connector.source.SourceReaderContext;
import org.apache.flink.api.connector.source.SourceSplit;
import org.apache.flink.api.connector.source.SplitEnumerator;
import org.apache.flink.api.connector.source.SplitEnumeratorContext;
import org.apache.flink.core.io.InputStatus;
import org.apache.flink.core.io.SimpleVersionedSerializer;

import java.io.IOException;
import java.nio.ByteBuffer;
import java.util.Collections;
import java.util.List;
import java.util.Random;
import java.util.concurrent.ArrayBlockingQueue;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicLong;
import java.util.concurrent.atomic.AtomicReference;

/**
 * The bid generator as a FLIP-27 {@link Source}, replacing the {@code RichParallelSourceFunction}
 * that Flink 2.0 removed.
 *
 * <p><b>Why this is a faithful port and not a {@code DataGeneratorSource}.</b> Flink ships a
 * generator source with rate limiting, and it would be less code — but it limits by BLOCKING, and
 * this benchmark deliberately does not. A producer thread generates at the rate the arrival
 * distribution asks for and offers into a bounded queue; when the queue is full the event is
 * DROPPED and counted. That decouples the arrival process from what the pipeline can absorb, which
 * is how an external source (a Kafka topic that keeps filling regardless) behaves, and it is what
 * keeps the pipeline saturated so that measured throughput is a measurement of CAPACITY rather than
 * of how fast the generator happened to be allowed to run. Swapping in rate limiting would change
 * what every throughput number in the campaign means.
 *
 * <p><b>Not replayable, on purpose.</b> The split carries no offset and {@link
 * Reader#snapshotState} returns the split unchanged: the events are synthetic and time-stamped at
 * generation, so replaying them after a restart would produce a different stream anyway. Restoring
 * simply resumes generating. Every rescale in this experiment restarts the source, and the arrival
 * curve is a function of wall-clock elapsed time, so this is the honest behaviour rather than a
 * shortcut.
 */
public class BidGeneratorSource implements Source<ConfigurableGraphJob.Bid, BidGeneratorSource.BidSplit, Void> {

    private static final long serialVersionUID = 1L;

    private final GraphConfig config;

    public BidGeneratorSource(GraphConfig config) {
        this.config = config;
    }

    @Override
    public Boundedness getBoundedness() {
        // The generator stops itself after config.durationSeconds by returning END_OF_INPUT, but the
        // job must still run under streaming semantics — the AdaptiveScheduler, and therefore every
        // rescale this thesis measures, only exists there.
        return Boundedness.CONTINUOUS_UNBOUNDED;
    }

    @Override
    public SourceReader<ConfigurableGraphJob.Bid, BidSplit> createReader(SourceReaderContext readerContext) {
        return new Reader(
                config,
                readerContext.getIndexOfSubtask(),
                readerContext.currentParallelism());
    }

    @Override
    public SplitEnumerator<BidSplit, Void> createEnumerator(
            SplitEnumeratorContext<BidSplit> enumContext) {
        return new Enumerator(enumContext);
    }

    @Override
    public SplitEnumerator<BidSplit, Void> restoreEnumerator(
            SplitEnumeratorContext<BidSplit> enumContext, Void checkpoint) {
        return new Enumerator(enumContext);
    }

    @Override
    public SimpleVersionedSerializer<BidSplit> getSplitSerializer() {
        return BidSplit.SERIALIZER;
    }

    @Override
    public SimpleVersionedSerializer<Void> getEnumeratorCheckpointSerializer() {
        return NO_ENUMERATOR_STATE;
    }

    // ------------------------------------------------------------------------

    /** One split per subtask; it carries nothing but its own index. */
    public static class BidSplit implements SourceSplit {

        static final SimpleVersionedSerializer<BidSplit> SERIALIZER =
                new SimpleVersionedSerializer<BidSplit>() {
                    @Override
                    public int getVersion() {
                        return 1;
                    }

                    @Override
                    public byte[] serialize(BidSplit split) {
                        return ByteBuffer.allocate(Integer.BYTES).putInt(split.index).array();
                    }

                    @Override
                    public BidSplit deserialize(int version, byte[] serialized) throws IOException {
                        if (version != 1) {
                            throw new IOException("Unknown split version " + version);
                        }
                        return new BidSplit(ByteBuffer.wrap(serialized).getInt());
                    }
                };

        private final int index;

        public BidSplit(int index) {
            this.index = index;
        }

        @Override
        public String splitId() {
            return "bids-" + index;
        }
    }

    /** The enumerator has no state worth checkpointing; the generator is a function of the clock. */
    private static final SimpleVersionedSerializer<Void> NO_ENUMERATOR_STATE =
            new SimpleVersionedSerializer<Void>() {
                @Override
                public int getVersion() {
                    return 1;
                }

                @Override
                public byte[] serialize(Void obj) {
                    return new byte[0];
                }

                @Override
                public Void deserialize(int version, byte[] serialized) {
                    return null;
                }
            };

    /** Hands each reader exactly one split as it registers. */
    private static class Enumerator implements SplitEnumerator<BidSplit, Void> {

        private final SplitEnumeratorContext<BidSplit> context;

        Enumerator(SplitEnumeratorContext<BidSplit> context) {
            this.context = context;
        }

        @Override
        public void start() {}

        @Override
        public void handleSplitRequest(int subtaskId, String requesterHostname) {
            // Splits are pushed in addReader, so a request can only mean the reader already has one.
        }

        @Override
        public void addSplitsBack(List<BidSplit> splits, int subtaskId) {
            // A failed reader is restarted and re-registers, which assigns it a fresh split.
        }

        @Override
        public void addReader(int subtaskId) {
            context.assignSplit(new BidSplit(subtaskId), subtaskId);
        }

        @Override
        public Void snapshotState(long checkpointId) {
            return null;
        }

        @Override
        public void close() {}
    }

    // ------------------------------------------------------------------------

    /**
     * Producer thread plus bounded queue, exactly as the original source function worked. {@link
     * #pollNext} drains the queue; the producer never waits for it.
     */
    private static class Reader implements SourceReader<ConfigurableGraphJob.Bid, BidSplit> {

        private final GraphConfig config;
        private final int subtaskIndex;
        private final int parallelism;

        private final AtomicLong generated = new AtomicLong();
        private final AtomicLong dropped = new AtomicLong();
        private final AtomicLong emitted = new AtomicLong();

        /**
         * Completed as soon as the queue stops being empty, so the runtime is told to come back
         * instead of being spun on. Replaced with a fresh pending future every time the reader runs
         * dry.
         */
        private final AtomicReference<CompletableFuture<Void>> available =
                new AtomicReference<>(CompletableFuture.completedFuture(null));

        private volatile boolean running = true;
        private ArrayBlockingQueue<ConfigurableGraphJob.Bid> queue;
        private Thread producer;
        private long start;
        private long end;

        Reader(GraphConfig config, int subtaskIndex, int parallelism) {
            this.config = config;
            this.subtaskIndex = subtaskIndex;
            this.parallelism = Math.max(1, parallelism);
        }

        @Override
        public void start() {
            final int peakRate;
            if (config.arrivalDistribution == GraphConfig.ArrivalDistribution.STEP) {
                peakRate = (int) (config.eventsPerSecond * config.stepHighRateFraction);
            } else if (config.arrivalDistribution == GraphConfig.ArrivalDistribution.SINE) {
                peakRate = (int) (config.eventsPerSecond * (1.0 + config.sineAmplitude));
            } else {
                peakRate = config.eventsPerSecond;
            }

            final int maxRatePerInstance = Math.max(1, peakRate / parallelism);
            queue = new ArrayBlockingQueue<>(maxRatePerInstance * 2);
            start = System.currentTimeMillis();
            end = start + config.durationSeconds * 1000L;

            System.out.printf(
                    "[Source-%d/%d] NON-BLOCKING: dist=%s baseRate=%d peakRate=%d qCap=%d maxAge=%dms%n",
                    subtaskIndex + 1,
                    parallelism,
                    config.arrivalDistribution,
                    config.eventsPerSecond,
                    peakRate,
                    maxRatePerInstance * 2,
                    config.maxEventAgeMs);

            producer = new Thread(this::produce, "bid-producer-" + subtaskIndex);
            producer.setDaemon(true);
            producer.start();
        }

        /**
         * Generates in fifty batches a second and re-reads the arrival curve once a second, so a
         * SINE or STEP distribution is followed without recomputing it per event.
         */
        private void produce() {
            final Random random = new Random();
            final ConfigurableGraphJob.Bid[] pool = new ConfigurableGraphJob.Bid[10_000];
            for (int i = 0; i < pool.length; i++) {
                pool[i] =
                        new ConfigurableGraphJob.Bid(
                                random.nextInt(1000),
                                random.nextInt(10000),
                                10 + random.nextDouble() * 990,
                                0L);
            }

            final int batchesPerSecond = 50;
            final long batchNanos = 1_000_000_000L / batchesPerSecond;
            long nextBatch = System.nanoTime();
            int poolIndex = 0;
            int ratePerInstance = Math.max(1, config.eventsPerSecond / parallelism);
            int batchSize = Math.max(1, ratePerInstance / batchesPerSecond);
            long lastRateUpdate = System.currentTimeMillis();

            while (running && System.currentTimeMillis() < end) {
                final long now = System.currentTimeMillis();
                if (now - lastRateUpdate >= 1000) {
                    final int globalRate = config.getInstantRate((now - start) / 1000.0);
                    ratePerInstance = Math.max(1, globalRate / parallelism);
                    batchSize = Math.max(1, ratePerInstance / batchesPerSecond);
                    lastRateUpdate = now;
                }

                for (int i = 0; i < batchSize; i++) {
                    final ConfigurableGraphJob.Bid template = pool[poolIndex];
                    poolIndex = (poolIndex + 1) % pool.length;
                    generated.incrementAndGet();
                    final ConfigurableGraphJob.Bid bid =
                            new ConfigurableGraphJob.Bid(template.auctionId, template.bidderId, template.price, now);
                    if (!queue.offer(bid)) {
                        // The pipeline cannot keep up. Dropping rather than blocking is the whole
                        // point: the arrival process must not be throttled by the thing under test.
                        dropped.incrementAndGet();
                    } else {
                        wakeUpReader();
                    }
                }

                nextBatch += batchNanos;
                final long sleepNanos = nextBatch - System.nanoTime();
                if (sleepNanos > 0) {
                    try {
                        Thread.sleep(sleepNanos / 1_000_000, (int) (sleepNanos % 1_000_000));
                    } catch (InterruptedException e) {
                        Thread.currentThread().interrupt();
                        return;
                    }
                } else {
                    nextBatch = System.nanoTime();
                }
            }
            // Let the reader notice the end instead of waiting out its timeout.
            wakeUpReader();
        }

        /**
         * Completes the reader's pending wake-up, if it has one. Deliberately does NOT swap in a
         * fresh future: this runs once per enqueued event, and allocating there would cost more
         * than the generation itself. The reader installs a new pending future when it next finds
         * the queue empty.
         */
        private void wakeUpReader() {
            final CompletableFuture<Void> pending = available.get();
            if (!pending.isDone()) {
                pending.complete(null);
            }
        }

        @Override
        public InputStatus pollNext(ReaderOutput<ConfigurableGraphJob.Bid> output) throws InterruptedException {
            final ConfigurableGraphJob.Bid bid = queue.poll();
            if (bid != null) {
                output.collect(bid);
                emitted.incrementAndGet();
                return InputStatus.MORE_AVAILABLE;
            }

            if (System.currentTimeMillis() >= end && queue.isEmpty()) {
                System.out.printf(
                        "[Source-%d] FINALIZADO en %.1fs: generated=%,d, emitted=%,d, dropped=%,d (%.1f%%)%n",
                        subtaskIndex + 1,
                        (System.currentTimeMillis() - start) / 1000.0,
                        generated.get(),
                        emitted.get(),
                        dropped.get(),
                        generated.get() > 0 ? dropped.get() * 100.0 / generated.get() : 0);
                return InputStatus.END_OF_INPUT;
            }

            // Ask to be called back rather than spinning. The producer completes this future the
            // moment it manages to enqueue something. compareAndSet rather than set, so a producer
            // that completes the old future between the read and the swap cannot be lost.
            final CompletableFuture<Void> current = available.get();
            if (current.isDone()) {
                available.compareAndSet(current, new CompletableFuture<>());
            }
            return InputStatus.NOTHING_AVAILABLE;
        }

        @Override
        public CompletableFuture<Void> isAvailable() {
            if (!queue.isEmpty() || System.currentTimeMillis() >= end) {
                return CompletableFuture.completedFuture(null);
            }
            return available.get();
        }

        @Override
        public List<BidSplit> snapshotState(long checkpointId) {
            // Nothing to resume from — see the class comment.
            return Collections.emptyList();
        }

        @Override
        public void addSplits(List<BidSplit> splits) {
            // One split per reader and it carries no work assignment; nothing to do.
        }

        @Override
        public void notifyNoMoreSplits() {}

        @Override
        public void close() throws Exception {
            running = false;
            if (producer != null) {
                producer.interrupt();
                producer.join(TimeUnit.SECONDS.toMillis(5));
            }
        }
    }
}
