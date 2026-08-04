# Assigner spread — Phase 2 of the Flink fork

How each `ThesisSlotAssigner` strategy distributes pipeline slices over TaskManagers
at a rescale. Produced by `scripts/measure-assigner-spread.sh`.

## What is measured

The slot assigner only has a choice when the pool holds more free slots than the job
needs slices — `slices = min(upperBound, freeSlots)`. Each repetition creates that
condition on purpose: submit at `-p 6`, then `PUT /jobs/{id}/resource-requirements`
with `upperBound=3`. The pool keeps its 6 slots while the job drops to 3 slices, so
the assigner picks which 3 of the 6 slots to use.

`balanced=yes` means the slices were dealt over as many TaskManagers as possible
(`tms_used == min(slices, tms_available)`), i.e. no TM carries an extra slice while
another sits idle.

## Valid comparison (2026-08-04)

All four runs below share the same controlled condition: **3 TaskManagers × 2 slots =
6 free slots, 3 slices**, 8 repetitions each.

| Strategy | Balanced | File |
|---|---|---|
| `STOCK` | 3/8 | `STOCK-20260804-182323.csv` |
| `DEFAULT` | 3/8 | `DEFAULT-20260804-173020.csv` |
| `ROUND_ROBIN` | 8/8 | `ROUND_ROBIN-20260804-172440.csv` |
| `LEAST_LOADED` | 8/8 | `LEAST_LOADED-20260804-173803.csv` |

`STOCK` is the baseline — it delegates to the assigner unpatched Flink would have
used (`StateLocalitySlotAssigner` at a rescale, `DefaultSlotAssigner` at submission).
`DEFAULT` is iteration order unconditionally, which stock Flink only does on a first
submission; it is a contrast, not the baseline.

Caveats: n=8. `STOCK` and `DEFAULT` scoring the same is an artifact of this workload —
TopSpeedWindowing has near-zero state, so state locality has little to anchor to and
most pairs fall through to Flink's arbitrary leftover distribution.

## Do NOT use these two

| File | Why |
|---|---|
| `DEFAULT-20260804-171529.csv` | 7/8 balanced, but measured with **5** TMs available |
| `ROUND_ROBIN-20260804-172218.csv` | 8/8 balanced, but measured with **3** TMs available |

They were run before the TM count was pinned. With 6 slots spread over 5 TMs there is
roughly 1 slot per TM, so almost any choice comes out balanced — the baseline landed
in the easy condition and the treatment in the hard one. Comparing them would have
understated the effect to the point of hiding it.

**Always pin the TM count (`kubectl scale deployment flink-taskmanager --replicas=N`)
and check the `tms_available` column before comparing strategies.**
