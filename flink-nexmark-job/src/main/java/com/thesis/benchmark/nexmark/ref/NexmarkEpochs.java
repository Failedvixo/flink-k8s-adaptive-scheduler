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

    /**
     * Where in the sequence the wall clock is, for a constant OR a time-varying rate.
     *
     * <p>WHY (2026-10-11). Training an agent that has to tell USING a resource from EXCEEDING it
     * needs the same cluster at several loads, and a scheduler that moves operators at run time
     * needs the load to change while the job runs. With a varying rate the epoch is the INTEGRAL
     * of the rate over time rather than time x rate. Every source computes that integral from the
     * same fixed origin with the same profile, so persons, auctions and bids stay on the same
     * epoch at every instant — the property Q8 and Q3 need to join — whenever each one opened.
     *
     * <p>Profiles: {@code CONSTANT}, or {@code STEPS:<seconds>:<rate>,<rate>,...} — total rates of
     * the query, held for {@code seconds} each, cycling. E.g. {@code STEPS:600:20000,45000} is ten
     * minutes at 20 000 ev/s and ten at 45 000, over and over, aligned to the fixed origin.
     */
    static final class Schedule implements java.io.Serializable {
        private static final long serialVersionUID = 1L;
        /** Fixed origin of the profile (2026-01-01T00:00Z): the same for every source. */
        static final long ORIGIN_MS = 1_767_225_600_000L;

        private final long epochsPerSecond;
        private final long stepMs;
        private final long[] permille;     // null = constant rate
        private final long periodIntegral; // integral of one full cycle, permille-ms

        private Schedule(long epochsPerSecond, long stepMs, long[] permille) {
            this.epochsPerSecond = epochsPerSecond;
            this.stepMs = stepMs;
            this.permille = permille;
            long sum = 0;
            if (permille != null) {
                for (long p : permille) {
                    sum += p * stepMs;
                }
            }
            this.periodIntegral = sum;
        }

        /**
         * @param dist     CONSTANT or STEPS:seconds:rate,rate,...
         * @param baseRate the query's total rate that {@code epochsPerSecond} corresponds to
         */
        static Schedule parse(String dist, long baseRate, long epochsPerSecond) {
            if (dist == null || dist.isEmpty() || "CONSTANT".equalsIgnoreCase(dist)) {
                return new Schedule(epochsPerSecond, 0, null);
            }
            final String[] parts = dist.split(":");
            if (parts.length != 3 || !"STEPS".equalsIgnoreCase(parts[0])) {
                throw new IllegalArgumentException(
                        "dist must be CONSTANT or STEPS:<seconds>:<rate>,<rate>,..., got " + dist);
            }
            final long step = Long.parseLong(parts[1].trim()) * 1000L;
            final String[] rates = parts[2].split(",");
            final long[] pm = new long[rates.length];
            for (int i = 0; i < rates.length; i++) {
                pm[i] = Math.round(1000.0 * Long.parseLong(rates[i].trim()) / Math.max(1, baseRate));
            }
            if (step <= 0 || rates.length == 0) {
                throw new IllegalArgumentException("bad STEPS profile: " + dist);
            }
            return new Schedule(epochsPerSecond, step, pm);
        }

        boolean isConstant() {
            return permille == null;
        }

        /** Rate multiplier (x1000) in force at {@code millis}. */
        long permilleAt(long millis) {
            if (permille == null) {
                return 1000;
            }
            final long t = Math.max(0, millis - ORIGIN_MS);
            return permille[(int) ((t / stepMs) % permille.length)];
        }

        /** Integral of the multiplier from the origin to {@code millis}, in permille x ms. */
        private long integral(long millis) {
            final long t = Math.max(0, millis - ORIGIN_MS);
            final long period = stepMs * permille.length;
            long acc = (t / period) * periodIntegral;
            long rem = t % period;
            int k = 0;
            while (rem >= stepMs) {
                acc += permille[k++] * stepMs;
                rem -= stepMs;
            }
            return acc + permille[k] * rem;
        }

        long epochAt(long millis) {
            if (permille == null) {
                return NexmarkEpochs.epochAt(millis, epochsPerSecond);
            }
            // integral/1000 is "effective milliseconds at the base rate"; the rest is as before.
            return (integral(millis) / 1000L) * epochsPerSecond / 1000L;
        }

        long timeOf(long epoch) {
            if (permille == null) {
                return NexmarkEpochs.timeOf(epoch, epochsPerSecond);
            }
            // The earliest millisecond at which the sequence reaches `epoch` (epochAt is
            // non-decreasing). A step at rate 0 makes it flat, which the search handles.
            long lo = ORIGIN_MS, hi = ORIGIN_MS + 4_000_000_000_000L;
            while (lo < hi) {
                final long mid = lo + (hi - lo) / 2;
                if (epochAt(mid) >= epoch) {
                    hi = mid;
                } else {
                    lo = mid + 1;
                }
            }
            return lo;
        }
    }

    /** The first epoch at or after {@code epoch} that belongs to subtask {@code index}. */
    static long alignUp(long epoch, int subtasks, int index) {
        final long remainder = Math.floorMod(epoch, (long) subtasks);
        final long step = Math.floorMod((long) index - remainder, (long) subtasks);
        return epoch + step;
    }
}
