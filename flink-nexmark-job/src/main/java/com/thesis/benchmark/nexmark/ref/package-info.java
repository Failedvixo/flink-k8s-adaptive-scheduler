/**
 * Nexmark sources built on the REFERENCE event generator, not on a reimplementation.
 *
 * <p><b>Why this package exists (2026-09-02).</b> The project's own
 * {@code NexmarkGenerator} draws every identifier from {@code rng.nextLong()}, so
 * {@code person.id} and {@code auction.seller} are independent uniform draws over
 * 2^31. Q8 joins exactly those two fields, so the expected number of matches in a
 * 10-second window at 75k events/s is about 0.3 — measured, the join emitted zero
 * records while holding state and firing empty windows. Q8 was never running Q8,
 * and Q3 and Q6 have the same latent defect since they also join on seller.
 *
 * <p><b>Provenance.</b> These sources are ported from the DS2 artifact
 * (github.com/strymon-system/ds2, Apache 2.0), which is the Flink DataStream
 * implementation of Nexmark used by DS2 (Kalavri et al., OSDI'18) and built on by
 * CAPSys (Wang et al., EuroSys'25) — the two systems this thesis compares against.
 * They wrap Apache Beam's own generator classes
 * ({@code org.apache.beam.sdk.nexmark.sources.generator}), so the event semantics
 * are the reference ones: identifiers derive from the event number rather than a
 * random draw, and an auction's seller is a person who actually exists, drawn with
 * the hot-seller concentration the benchmark specifies.
 *
 * <p><b>Deviations from DS2's port, and why.</b> DS2 pinned Beam 2.3.0 and Flink
 * 1.4.1 on Java 8; this uses a Beam version that compiles on Java 17 against Flink
 * 2.3, which moved {@code RichParallelSourceFunction} into the {@code legacy}
 * package. DS2's sources also generate the SAME event sequence in every parallel
 * subtask, which is harmless at their default parallelism of 1 and duplicates every
 * person at ours; the event space is split per subtask here instead.
 */
package com.thesis.benchmark.nexmark.ref;
