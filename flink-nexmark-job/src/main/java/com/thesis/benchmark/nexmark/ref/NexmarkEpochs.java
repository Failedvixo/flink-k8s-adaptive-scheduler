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

    /** The epoch the wall clock is in at {@code millis}. */
    static long epochAt(long millis, long epochsPerSecond) {
        return millis * epochsPerSecond / 1000L;
    }

    /** When an epoch happens, in epoch milliseconds — this is the events' event time. */
    static long timeOf(long epoch, long epochsPerSecond) {
        return epoch * 1000L / epochsPerSecond;
    }

    /** The first epoch at or after {@code epoch} that belongs to subtask {@code index}. */
    static long alignUp(long epoch, int subtasks, int index) {
        final long remainder = Math.floorMod(epoch, (long) subtasks);
        final long step = Math.floorMod((long) index - remainder, (long) subtasks);
        return epoch + step;
    }
}
