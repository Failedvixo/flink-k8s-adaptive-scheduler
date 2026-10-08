package com.thesis.benchmark.nexmark.ref;

/**
 * A wall-clock-anchored position in Beam's Nexmark event sequence, shared by every source.
 *
 * <p>WHY (2026-09-18). Beam's generator is ONE interleaved sequence: out of every 50 event
 * numbers, one is a person, three are auctions and forty-six are bids, and the references between
 * them are arithmetic on that number — an auction at event {@code m} names a seller whose id is
 * derived from {@code m}. Our Q8 splits persons and auctions into two sources, and each kept its
 * OWN counter and generated a record of its type for EVERY number. Two defects followed, both
 * measured on 2026-09-18 at 20000 rec/s with no backpressure:
 *
 * <ul>
 *   <li>a person was emitted for every event number, and fifty consecutive numbers map to one
 *       person id, so each person appeared ~50 times — the join emitted 46888 pairs/s from 20000
 *       inputs/s in the first minute;
 *   <li>the auction counter ran three times faster than the person counter (it emits three times
 *       as many records), so auctions soon named sellers the person source had not reached yet,
 *       and from the second minute on the join emitted exactly ZERO, forever.
 * </ul>
 *
 * The sink therefore received ~15000–20000 rec/s in campaigns measured a minute after a rescale
 * and ~1700 in those measured after six, which is how it was found. Every campaign since the
 * migration measured a join that stored its input and matched nothing.
 *
 * <p>The fix is to walk the SAME sequence in both sources, each emitting only the numbers of its
 * type. The sequence position is derived from the wall clock, so at any instant every source — of
 * either type, any subtask, before or after a restart — is at the same epoch. Consequences: ids
 * and seller references line up as in Beam; event time IS the wall clock; and the stale-event
 * skip reduces to jumping to the current epoch, which is the same epoch for both sources, so
 * skipping can no longer pull them apart.
 *
 * <p>Subtask {@code i} of {@code p} owns the epochs {@code e} with {@code e mod p == i}, which
 * keeps every id unique across subtasks without depending on how Beam splits a config.
 */
final class NexmarkEpochs {

    private NexmarkEpochs() {}

    /**
     * How often a source wakes up to emit what is due (2026-10-08). It was once a second, so every
     * event waited on average half a second inside its source before entering the graph — about
     * 0.5 of Q3's measured 0.75 s of per-record latency — and the load arrived as one burst per
     * second. A tenth of a second keeps the generator's cost negligible (a few ms per second) and
     * lets latency differences of a few hundred milliseconds show.
     */
    static final long EMIT_PERIOD_MS = 100L;

    /** The epoch the wall clock is in at {@code millis}. */
    static long epochAt(long millis, long epochsPerSecond) {
        return millis * epochsPerSecond / 1000L;
    }

    /** When an epoch happens, in epoch milliseconds — this is the events' event time. */
    static long timeOf(long epoch, long epochsPerSecond) {
        return epoch * 1000L / epochsPerSecond;
    }

    /**
     * Wall-clock milliseconds that never go backwards and never jump: the wall clock read ONCE,
     * then advanced by {@link System#nanoTime()}.
     *
     * <p>WHY (2026-10-04). WSL2 corrects its clock in steps. Q5's bid source was measured with a
     * generator "busy" time of -10243 ms: the wall clock went back ten seconds inside one pass of
     * the emission loop, the loop then slept 1000 - (-10243) ms = eleven seconds, and because the
     * sequence is anchored to the wall clock it had to re-live those ten seconds emitting nothing.
     * Forward steps did the opposite and tripped the stale-event skip. The source delivered
     * between 71% and 90% of the requested rate with every operator downstream idle, varying
     * from job to job with the corrections.
     *
     * <p>Anchoring once keeps what the wall clock is needed for — every source of a job opens
     * within milliseconds of the others on the same host, so they still agree on the epoch —
     * and takes the steps out of everything after. A restart re-anchors, as all sources restart
     * together.
     */
    static final class Clock {
        private final long wallAtStart = System.currentTimeMillis();
        private final long nanoAtStart = System.nanoTime();

        long nowMillis() {
            return wallAtStart + (System.nanoTime() - nanoAtStart) / 1_000_000L;
        }
    }

    /** The first epoch at or after {@code epoch} that belongs to subtask {@code index}. */
    static long alignUp(long epoch, int subtasks, int index) {
        final long remainder = Math.floorMod(epoch, (long) subtasks);
        final long step = Math.floorMod((long) index - remainder, (long) subtasks);
        return epoch + step;
    }
}
