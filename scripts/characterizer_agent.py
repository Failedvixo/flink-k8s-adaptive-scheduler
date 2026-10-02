#!/usr/bin/env python3
"""SARSA agent that DECIDES the placement, slice by slice.

WHAT IT IS FOR. The thesis question is whether an agent can learn to characterise what a
streaming operator needs and place it accordingly, without being handed a profile. The
profile route was tried first and is the thing being replaced: Flink cannot measure busy
time for a legacy SourceFunction, so the published profile priced the Beam generators at
zero, and LPT following it faithfully went from +11.2% over STOCK to -11.0%. The published
cost is also machine-dependent (the same operator looks dearer on the slow machine) and
counts time waiting on RocksDB as CPU. So the agent does not tune LPT's input — it takes
LPT's job.

HOW THE DECISION REACHES THE SCHEDULER. Placement happens inside the JobManager, so the
fork gained an arm, RL, that applies a plan published at /var/thesis/assignment; the same
arm writes /var/thesis/slices on every decision, because slices are built from the slot
sharing groups and are invisible over REST — without that file an agent can neither compute
a slice's features nor respect a machine's capacity. A plan that does not check out is
rejected WHOLE and the fork falls back to LPT, so a bad plan costs an episode, never a
corrupt placement.

THE FORMULATION, and the budget each choice answers to:

* ONE DECISION PER SLICE, not one plan per episode. The action is "which machine", three
  values, instead of an assignment of eight slices to three machines. An episode is then
  eight training samples rather than one, and 100 episodes — about six hours on this
  cluster — give ~800 updates over ~42 cells.

* SLICES ARE DECIDED HEAVIEST FIRST, the order LPT uses. The order has to be fixed for the
  state to mean anything: "which machines still have room" only makes sense relative to a
  known sequence.

* STATE = (this slice's load, relative to the heaviest slice of the job) x (which of the
  three machine ranks still have a free slot). Nothing in it names a vertex, a query or a
  machine, which is what lets a table trained on Q8 be evaluated on Q5 — the transfer
  matrix the professor asked for.

* REWARD = source throughput over the window, against a running mean kept PER WIDTH. A job
  at 12 slices and the same job at 8 are different problems; comparing their rec/s would
  reward the width. This also survives the random operator scaling that is still to come.

* CREDIT IS SHARED: every decision of an episode gets the episode's reward. Which of the
  eight moved the number is not identified. That is the known weakness, stated here rather
  than discovered in the results.

STILL AN ORACLE, and worth saying plainly: the agent ranks the machines by the published
speed vector. It characterises OPERATORS from metrics; characterising MACHINES from metrics
is the next step, not this one.

Run it next to a driver whose arm is RL:
  CHARACTERISER=1 ARMS="RL" ... scripts/run-placement-experiment.sh
"""
import argparse
import csv
import json
import math
import random
import statistics
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import arm_controller as ac  # noqa: E402  (transport, epochs, source counters)

GENERATOR_SUFFIX = "generatorBusyMsPerSecond"
SLICES_FILE = "/var/thesis/slices"
PLAN_FILE = "/var/thesis/assignment"
SPEEDS_FILE = "/var/thesis/speeds"


# ---------------------------------------------------------------------------
# the node's files
# ---------------------------------------------------------------------------
def node_command(node, argv, content=None, timeout=90):
    """Run a command on the minikube node from a BACKGROUND process.

    docker first (2026-09-16): `minikube ssh` reaches for the controlling terminal, and the
    driver starts this agent in the background — the first version was stopped outright by
    SIGTTIN and sat there for twelve minutes without printing a line. `docker exec` needs no
    terminal and runs as root. minikube ssh stays as a fallback, and both get a deadline so a
    slow call costs one episode instead of the pilot.
    """
    attempts = [["docker", "exec", "-i", node] + argv,
                ["minikube", "ssh", "-n", node, "--", " ".join(argv)]]
    errors = []
    for cmd in attempts:
        try:
            done = subprocess.run(cmd, input=content, text=True, capture_output=True,
                                  timeout=timeout, start_new_session=True)
            if done.returncode == 0:
                return done.stdout, ""
            errors.append(f"{cmd[0]} rc={done.returncode} {done.stderr.strip()[:160]}")
        except FileNotFoundError:
            errors.append(f"{cmd[0]} no instalado")
        except subprocess.TimeoutExpired:
            errors.append(f"{cmd[0]} timeout {timeout}s")
    return None, " | ".join(errors)


def read_layout(node):
    """{'slices': [[(vertexId, subtask), ...]], 'tms': [(id, free_slots)], 'raw': text} or None."""
    text, _ = node_command(node, ["cat", SLICES_FILE])
    if not text:
        return None
    slices, tms = {}, []
    for line in text.splitlines():
        parts = line.split()
        if not parts:
            continue
        if parts[0] == "tm" and len(parts) >= 3:
            tms.append((parts[1], int(parts[2])))
        elif parts[0] == "slice" and len(parts) >= 3:
            members = []
            for token in parts[2:]:
                vid, _, sub = token.rpartition(":")
                members.append((vid, int(sub)))
            slices[int(parts[1])] = members
    if not slices or not tms:
        return None
    return {"slices": [slices[i] for i in sorted(slices)], "tms": tms, "raw": text}


def read_speeds(node):
    """{taskmanager id: speed}. The one oracle left, and only for ranking machines."""
    text, _ = node_command(node, ["cat", SPEEDS_FILE])
    speeds = {}
    for line in (text or "").splitlines():
        parts = line.split()
        if len(parts) == 2:
            try:
                speeds[parts[0]] = float(parts[1])
            except ValueError:
                pass
    return speeds


def publish_plan(sections, node):
    """One section per width, so the job can cycle between them without a gap.

    A width the agent has not measured yet has no section, and the fork falls back to LPT for
    it — announced in the JobManager log, not silently.
    """
    out = []
    for count in sorted(sections):
        out.append(f"slices={count}")
        # By TASK, not by slice index (2026-09-17): the group order changes between jobs, and a
        # plan published after measuring job k is applied in job k+1.
        for task, tm in sorted(sections[count].items()):
            out.append(f"task {task} {tm}")
    content = "\n".join(out) + "\n"
    _, error = node_command(
        node, ["sh", "-c", f"cat > {PLAN_FILE}.tmp && mv -f {PLAN_FILE}.tmp {PLAN_FILE}"
                           f" && chmod 644 {PLAN_FILE}"], content=content)
    return error == "", error


# ---------------------------------------------------------------------------
# observation
# ---------------------------------------------------------------------------
def sink_delay_ms(base, jid, detail):
    """How far the sinks' event time trails the wall clock, in ms, or None.

    THE SECOND TERM OF THE REWARD (2026-09-25). Over 367 credited episodes the end-to-end
    delay is both the best predictor of throughput among everything measured (r = -0.70,
    against -0.25 for backpressure and +0.07 for load imbalance) and by far the quietest:
    repeating the same placement moved it 1.5% where throughput moved 2.8% and imbalance 92%.
    It is not extra information — it is largely the same signal with less noise on it, and
    with ~50 credited episodes per session the variance of the reward is what limits
    learning. Penalising it also states the objective honestly as throughput AND latency,
    rather than smuggling in a proxy that is supposed to stand for throughput.

    The watermark is a MINIMUM over the sink's subtasks, so one lagging subtask sets the
    figure — which is the behaviour wanted here, since that is also what a user waits for.
    """
    _sources, sinks = ac.graph_roles(detail)
    watermarks = []
    for vertex in detail.get("vertices", []):
        if vertex["id"] not in sinks:
            continue
        for index in range(int(vertex.get("parallelism", 0))):
            values = ac.subtask_metrics(base, jid, vertex["id"], index)
            if not values:
                continue
            mark = values.get("currentInputWatermark")
            # Long.MIN_VALUE until the first watermark arrives; treating it as a timestamp
            # yields a delay of ~2.9e11 seconds and poisons the baseline for the whole run.
            if mark is not None and mark > ac.WATERMARK_FLOOR_MS:
                watermarks.append(mark)
    if not watermarks:
        return None
    return max(0.0, time.time() * 1000.0 - min(watermarks))


def generator_metric_ids(base, jid, detail):
    sources, _ = ac.graph_roles(detail)
    ids = {}
    for vid in sources:
        listing = ac.rest(base, f"/jobs/{jid}/vertices/{vid}/subtasks/0/metrics") or []
        match = [m["id"] for m in listing if m.get("id", "").endswith(GENERATOR_SUFFIX)]
        if match:
            ids[vid] = match[0]
    return ids


DISK_SUFFIX = ".rocksdb_bytes_written"
STALL_SUFFIX = ".rocksdb_stall_micros"


def disk_metric_ids(base, jid, detail, suffix=DISK_SUFFIX):
    """{vertexId: metric id} for the vertices that keep RocksDB state.

    Only a vertex with keyed state has a RocksDB instance, so only it exposes the counter: in Q8
    that is the join and nothing else. A vertex missing from this map writes nothing, which is
    the truth, not a gap.
    """
    ids = {}
    for v in detail.get("vertices", []):
        listing = ac.rest(base, f"/jobs/{jid}/vertices/{v['id']}/subtasks/0/metrics") or []
        match = [m["id"] for m in listing if m.get("id", "").endswith(suffix)]
        if match:
            ids[v["id"]] = match[0]
    return ids


def disk_bytes(base, jid, detail, disk_ids):
    """{(vertexId, subtask): cumulative bytes RocksDB has written} for the stateful vertices."""
    out = {}
    for v in detail.get("vertices", []):
        mid = disk_ids.get(v["id"])
        if not mid:
            continue
        for i in range(int(v.get("parallelism", 0))):
            payload = ac.rest(base, f"/jobs/{jid}/vertices/{v['id']}/subtasks/{i}/metrics?get={mid}")
            for m in payload or []:
                try:
                    out[(v["id"], i)] = float(m["value"])
                except (KeyError, TypeError, ValueError):
                    pass
    return out


def slice_disk_rates(layout, start, end, elapsed):
    """Bytes per second each slice writes to RocksDB over the window — the second dimension.

    THE SIGNAL busyTime NEVER WAS (2026-09-29). It is cumulative, so a difference over the
    window is honest; it has no ceiling, so a saturated writer does not read as a cheap one; and
    it belongs to the operator rather than to where it happens to run. Its meaning is also
    physical: capping one TaskManager's disk bandwidth to half of what the join writes cut the
    job's throughput by 52%, with the placement otherwise identical. That is what makes "which
    machine" depend on WHAT a slice is heavy in, not only on how heavy it is.
    """
    rates = []
    for members in layout["slices"]:
        total = 0.0
        for member in members:
            if member in start and member in end and end[member] >= start[member]:
                total += (end[member] - start[member]) / max(1e-6, elapsed)
        rates.append(total)
    return rates


def subtask_loads(base, jid, detail, generator_ids):
    """{(vertexId, subtask): busy ms/s}, with the generator gauge standing in for sources."""
    jobs = {}
    for v in detail.get("vertices", []):
        for i in range(int(v.get("parallelism", 0))):
            jobs[(v["id"], i)] = (ac.subtask_metrics, (base, jid, v["id"], i))
            gen = generator_ids.get(v["id"])
            if gen:
                jobs[(v["id"], i, "gen")] = (
                    ac.rest,
                    (base, f"/jobs/{jid}/vertices/{v['id']}/subtasks/{i}/metrics?get={gen}"))
    results = ac.fetch_parallel(jobs)

    loads = {}
    for key, value in results.items():
        if len(key) == 3 or value is None:
            continue
        busy = value.get("busyTimeMsPerSecond", 0.0)
        gen = results.get(key + ("gen",))
        if gen:
            try:
                busy = max(busy, float(gen[0]["value"]))
            except (KeyError, IndexError, TypeError, ValueError):
                pass
        loads[key] = busy
    return loads


def operator_profile(values):
    """{(vertex, subtask): x} -> every subtask carries the MAX over its operator's subtasks.

    CHARACTERISE OPERATORS, NOT SUBTASKS (2026-10-01). On the fast-disk bench the frozen agent
    lost two of four evaluation passes the same way: the two join subtasks measured 864 and 302
    ms/s in the observation repetition — same operator, same keys, the difference made entirely
    by where the PREVIOUS placement had put them and how backpressure fell. The 302 one arrived
    as `LOW|disk=Y`, a row training had barely visited, fell through to "fastest free" and landed
    on the machine with the capped disk; both passes dropped from ~34k to ~16k rec/s. What a
    subtask measures is partly a property of its placement; what an operator needs is not. The
    max is used because the lighter reading is the one distorted by being starved, and because
    saturation in ANY subtask means the operator's demand reached the ceiling somewhere.
    """
    peak = {}
    for (vertex, _sub), v in values.items():
        peak[vertex] = max(peak.get(vertex, 0.0), v)
    return {(vertex, sub): peak[vertex] for (vertex, sub) in values}


def slice_loads(layout, loads, saturation_cut=None):
    """[(cost, saturated)] per slice: the sum of its subtasks, and whether any is at its ceiling.

    THE SUM IS NOT ENOUGH, AND THE 2026-09-22 CAMPAIGN SHOWS WHY IT COSTS 33%. Busy time is
    bounded by 1000 ms per second of wall clock, because that is what a second is. An
    operator that needs more than one machine-second per second reports the ceiling, not its
    demand: the Q8 join, alone in its slice and pinned at 938 ms/s, was outranked by the
    auction stage summing three unsaturated vertices to 1065. The agent therefore placed the
    auction stage first, filled the fast machine with it, and sent the job's single most
    expensive operator to the one-core machine.

    A saturated subtask is not evidence of a small demand; it is evidence that the demand
    exceeded what the machine could give, and the excess is unmeasurable. So saturation is
    carried separately and used to ORDER the decisions — a slice at its ceiling is decided
    first, whatever the sums say — rather than folded into a number that cannot express it.
    """
    out = []
    for members in layout["slices"]:
        values = [loads.get(member, 0.0) for member in members]
        saturated = bool(saturation_cut and any(v >= saturation_cut for v in values))
        out.append((sum(values), saturated))
    return out


# ---------------------------------------------------------------------------
# learner
# ---------------------------------------------------------------------------
class SliceSarsa:
    """One table shared by every slice: the policy is a function of what a slice looks like
    and of what room is left, never of which slice or which query it is."""

    def __init__(self, alpha, gamma, path, min_visits=0, ucb=0.0):
        self.alpha, self.gamma, self.path = alpha, gamma, path
        self.min_visits = min_visits
        self.ucb = ucb
        self.q, self.n = {}, {}
        if path and path.exists():
            stored = json.loads(path.read_text())
            # Two formats: the flat {state: {action: value}} written before visit counts
            # existed, and {"q": ..., "n": ...} written since. An old table loads with every
            # count at zero, which is honest — we do not know how often those cells were seen.
            if isinstance(stored, dict) and "q" in stored and isinstance(stored.get("q"), dict):
                self.q, self.n = stored["q"], stored.get("n", {})
            else:
                self.q = stored

    def value(self, state, action):
        return self.q.get(state, {}).get(str(action), 0.0)

    def visits(self, state, action):
        return self.n.get(state, {}).get(str(action), 0)

    def choose(self, state, allowed, epsilon):
        if not allowed:
            return None
        if self.ucb > 0:
            return self._choose_ucb(state, allowed)
        if random.random() < epsilon:
            return random.choice(allowed)
        # A CELL SEEN ONCE MUST NOT DECIDE (2026-09-25). The campaign of 2026-09-22 lost 33%
        # of its throughput to `HIGH|free=011` preferring the one-core machine over the
        # middle one — a preference that contradicts the equivalent row for light slices and
        # came from a handful of episodes. Nothing distinguished it from a converged cell,
        # because the table stored values and not how often they had been seen. Below
        # min_visits an action is not consulted; if none of the available actions clears the
        # bar the fall-back is the tie-break below, which is the fastest machine still free.
        trusted = [a for a in allowed if self.visits(state, a) >= self.min_visits]
        if trusted:
            # Ties go to the fastest machine still available.
            return max(trusted, key=lambda a: (self.value(state, a), -a))
        # Nothing in this row has been seen enough times, so the values are not evidence and
        # are ignored outright — falling back to them is what put the join on the one-core
        # machine. The prior is the fastest machine still free: what an uninformed policy
        # should do, and what LPT would do.
        #
        # BUT FIRST, BACK OFF TO THE SLICE'S PROFILE (2026-09-30). The first frozen evaluation
        # on the disk bench put a join subtask on the disk-capped machine through exactly this
        # fall-back: that subtask ran at 161 ms/s instead of saturated, so it arrived as
        # `LOW|disk=Y|free=012`, a row training never visited — and "fastest free" was medium.
        # The table DID know the answer, just not in that row: every visited `disk=Y` row rates
        # medium below the alternatives. The free-slot mask is the least general part of the
        # state, so when the exact row is empty the agent asks what it learned about this
        # load|disk profile over ALL masks, weighting each row by its visits, and uses that
        # before the blind prior. That is the generalisation the profile features exist for.
        pooled = self._pooled(state, allowed)
        if pooled:
            return max(pooled, key=lambda a: (pooled[a], -a))
        # Second level (2026-10-01): if even load|disk has too little evidence, keep only the
        # resource the slice is heavy in. "Writes to disk" is what decides the machine on a
        # bench with a capped disk, and it is far better sampled than any one load|disk pair.
        pooled = self._pooled(state, allowed, level="disk")
        if pooled:
            return max(pooled, key=lambda a: (pooled[a], -a))
        return min(allowed)

    def _pooled(self, state, allowed, level="load|disk"):
        """Visit-weighted mean Q per allowed action over every row sharing this profile —
        load|disk by default, or only the disk flag with level='disk'."""
        profile = state.rsplit("|free=", 1)[0] + "|free="
        disk_tag = next((f for f in state.split("|") if f.startswith("disk=")), None)
        total, count = {}, {}
        for row, visits in self.n.items():
            if level == "disk":
                if disk_tag is None or disk_tag not in row.split("|"):
                    continue
            elif not row.startswith(profile):
                continue
            for a in allowed:
                n = visits.get(str(a), 0)
                if n:
                    total[a] = total.get(a, 0.0) + n * self.value(row, a)
                    count[a] = count.get(a, 0) + n
        return {a: total[a] / count[a] for a in count if count[a] >= self.min_visits}

    def _choose_ucb(self, state, allowed):
        """UCB1: value plus a bonus that shrinks as an action is tried.

        WHY IT REPLACED EPSILON FOR TRAINING (2026-09-29). The disk-capped run learned its values
        correctly and still could not act on them. With the auction stage deciding first, the
        table held `fast = -0.482` from 16 visits — it KNEW that was bad — while the right move,
        sending the auctions to the medium machine so the fast one stays free for the join, had
        NEVER been tried in that row. Epsilon had decayed to 5% before the alternative gathered
        enough visits to be trusted, so the agent kept choosing the option it had measured as
        bad because it was the only one it had measured at all.

        Random exploration spends its budget uniformly; this spends it on what the agent knows
        least. An untried action is taken first, outright. After that the bonus
        c * sqrt(ln N(s) / N(s,a)) lets a poorly known action beat a well known bad one — which is
        exactly "I know this is bad but not what is better".
        """
        untried = [a for a in allowed if self.visits(state, a) == 0]
        if untried:
            return min(untried)                      # ties to the fastest machine, as the prior
        total = sum(self.visits(state, a) for a in allowed)
        return max(allowed, key=lambda a: (
            self.value(state, a)
            + self.ucb * math.sqrt(math.log(max(total, 1)) / self.visits(state, a)),
            -a))

    def update(self, state, action, reward, next_state, next_action):
        old = self.value(state, action)
        future = 0.0 if next_state is None or next_action is None else self.value(
            next_state, next_action)
        self.q.setdefault(state, {})[str(action)] = old + self.alpha * (
            reward + self.gamma * future - old)
        self.n.setdefault(state, {})[str(action)] = self.visits(state, action) + 1

    def save(self):
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps({"q": self.q, "n": self.n},
                                            indent=2, sort_keys=True))


def room_bucket(free, capacity):
    """How much of a machine is still free, in three levels: 2 mostly, 1 partly, 0 none.

    WHY NOT A SINGLE BIT PER MACHINE, WHICH IS WHAT THIS WAS UNTIL 2026-09-27. The old key
    asked only "does this machine still have a slot", and with capacities 2/4/6 over eight
    slices that made `free=011` govern SIX CONSECUTIVE DECISIONS: once the fast machine fills,
    the state stops changing, so a deterministic policy necessarily repeats one answer six
    times. The agent was not choosing badly — a mixed assignment was not expressible. It could
    only pour everything onto one machine, and the plan it produced (six slices on the
    one-core machine, the two-core machine left empty) was the ONLY shape its state could
    describe. Measured cost against the same plan with two of those slices moved: -16.5% of
    throughput, +39% backpressure, p~0.001.

    The levels are RELATIVE TO THE MACHINE'S OWN CAPACITY, so they advance as it fills — six
    free of six reads "mostly", four of six reads "partly" — and so that the key keeps meaning
    the same thing on a cluster with different slot counts, which is what makes the table
    transferable at all.
    """
    if free <= 0:
        return "0"
    return "2" if capacity and free >= 2 * capacity / 3 else "1"


def decide(agent, slices, capacity, epsilon, busy_cut, disk=None, disk_cut=0.5,
           disk_floor=100_000.0):
    """Place every slice, most demanding first. `slices` is [(cost, saturated)].

    `disk` is each slice's RocksDB write rate in bytes/s. It enters the STATE, not the order:
    the order stays "most CPU-demanding first", so the disk feature changes only which row is
    consulted — which isolates its effect and keeps the comparison with earlier tables honest.

    Returns (plan, trajectory).
    """
    costs = [c for c, _ in slices]
    peak = max(costs) if costs and max(costs) > 0 else 1.0
    disk = disk or [0.0] * len(slices)
    # Relative to the heaviest writer, like the CPU cut, so the key means the same thing at any
    # rate; the absolute floor keeps a job in which nothing writes from labelling noise as disk.
    disk_peak = max(disk) if disk else 0.0
    room = list(capacity)
    plan, trajectory = {}, []
    # Saturated slices go first regardless of their sums, then by cost. See slice_loads.
    for index in sorted(range(len(slices)),
                        key=lambda i: (not slices[i][1], -costs[i], i)):
        allowed = [rank for rank, left in enumerate(room) if left > 0]
        mask = "".join(room_bucket(left, capacity[rank]) for rank, left in enumerate(room))
        # Saturated counts as HIGH by definition: it is at the ceiling of what it was given.
        heavy = slices[index][1] or costs[index] / peak >= busy_cut
        writes = disk_peak > disk_floor and disk[index] / disk_peak >= disk_cut
        state = f"load={'HIGH' if heavy else 'LOW'}|disk={'Y' if writes else 'N'}|free={mask}"
        action = agent.choose(state, allowed, epsilon)
        if action is None:
            return None, []
        room[action] -= 1
        plan[index] = action
        # The slice index travels with the decision so its OWN outcome can be credited to it —
        # see the local-credit block in main().
        trajectory.append((state, action, index))
    return plan, trajectory


# ---------------------------------------------------------------------------
# loop
# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--rest", default=ac.DEFAULT_REST)
    ap.add_argument("--jm-pod", default="")
    ap.add_argument("--namespace", default="flink")
    ap.add_argument("--node", default="minikube")
    ap.add_argument("--warmup", type=float, default=60.0)
    ap.add_argument("--window", type=float, default=120.0)
    ap.add_argument("--samples", type=int, default=5)
    ap.add_argument("--poll-interval", type=float, default=5.0)
    ap.add_argument("--alpha", type=float, default=0.3)
    ap.add_argument("--gamma", type=float, default=0.5)
    ap.add_argument("--epsilon", type=float, default=0.3)
    ap.add_argument("--epsilon-decay", type=float, default=0.97)
    ap.add_argument("--epsilon-min", type=float, default=0.05)
    ap.add_argument("--saturation-cut", type=float, default=900.0,
                    help="a subtask at or above this many ms/s is pinned against the "
                         "one-second ceiling, so its slice is decided first")
    ap.add_argument("--busy-cut", type=float, default=0.5,
                    help="HIGH load = at least this fraction of the heaviest slice")
    ap.add_argument("--baseline-decay", type=float, default=0.8)
    ap.add_argument("--missing-tolerance", type=int, default=4,
                    help="consecutive polls with no running job before the published plan is "
                         "forgotten; guards against a single failed REST call")
    ap.add_argument("--only-parallelism", default=None,
                    help="comma-separated list of parallelisms to learn from; epochs at any "
                         "other width are skipped without spending a window. Several values "
                         "are what random operator scaling needs: training at one width makes "
                         "the table memorise a single slice geometry instead of generalising "
                         "over them, which is the overfitting the professor's plan avoids.")
    ap.add_argument("--min-visits", type=int, default=3,
                    help="an action seen fewer times than this is not consulted when acting "
                         "greedily; 0 restores the old behaviour")
    ap.add_argument("--profile-by", choices=["operator", "subtask"], default="operator",
                    help="describe a slice by its operators' peak over their subtasks "
                         "(default, since 2026-10-01) or by each subtask's own reading")
    ap.add_argument("--ucb", type=float, default=0.0,
                    help="UCB exploration coefficient while training; replaces epsilon-greedy "
                         "and is ignored under --freeze. 0 keeps epsilon-greedy.")
    ap.add_argument("--local-credit", type=float, default=0.0,
                    help="weight of each decision's own RocksDB stall share, subtracted from the "
                         "shared reward for that decision only; 0 keeps credit fully shared")
    ap.add_argument("--latency-weight", type=float, default=0.0,
                    help="subtract this times the relative end-to-end delay from the reward; "
                         "0 keeps the reward on throughput alone")
    ap.add_argument("--qtable", default=None)
    ap.add_argument("--freeze", action="store_true",
                    help="evaluate: act greedily, never update the table")
    ap.add_argument("--out-dir", default="results/characterizer")
    ap.add_argument("--seed", type=int, default=None)
    args = ap.parse_args()

    ac._JM_POD = args.jm_pod or None
    ac._NAMESPACE = args.namespace
    if args.seed is not None:
        random.seed(args.seed)

    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    qpath = Path(args.qtable) if args.qtable else out / "characterizer-qtable.json"
    # UCB only while LEARNING: exploring during a frozen evaluation would mix the policy with its
    # own search, and the evaluation is supposed to measure the policy.
    agent = SliceSarsa(args.alpha, args.gamma, qpath, args.min_visits,
                       ucb=0.0 if args.freeze else args.ucb)
    csv_path = out / f"agent-episodes-{time.strftime('%Y%m%d-%H%M%S')}.csv"

    print("=" * 60)
    print(f"  Agente — {'EVALUACION (tabla congelada)' if args.freeze else 'ENTRENAMIENTO'}")
    print("  decide el emplazamiento slice por slice; brazo del fork: RL")
    print(f"  tabla: {qpath}")
    print("=" * 60, flush=True)

    fields = ["epoch", "time", "slices", "rps", "baseline", "e2e_ms", "e2e_base",
              "reward", "credited", "epsilon", "plan", "applied"]
    with csv_path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        handle.flush()

        epoch, current_key, epoch_started = 0, None, 0.0
        measured = True
        plans = {}
        pending = {}
        baselines = {}
        delay_baselines = {}

        measured_widths = {int(x) for x in str(args.only_parallelism).replace(",", " ").split()
                           } if args.only_parallelism else set()
        missing = 0
        while True:
            jid = ac.running_job(args.rest)
            if jid is None:
                # A SINGLE FAILED REST CALL IS NOT A VANISHED JOB (2026-09-26). ac.rest() does
                # not retry — deliberately, because this loop polls — but the reaction here was
                # to forget the published plan, and forgetting it makes the NEXT episode
                # uncreditable: the trajectory that produced the placement under measurement is
                # gone. Measured on the training run of that night: three width-8 episodes,
                # every one of them with the publish time comfortably before the epoch it
                # should have been credited to, and not one credited. Over nine minutes the
                # agent makes about a hundred calls through `kubectl exec`; one of them failing
                # is a certainty, and the plan is still on the node either way.
                missing += 1
                if missing >= args.missing_tolerance:
                    current_key, measured = None, True
                    pending.clear()
                time.sleep(args.poll_interval)
                continue
            missing = 0

            key = ac.epoch_key(args.rest, jid)
            if key is not None and key != current_key:
                current_key = key
                epoch_started = ac.epoch_started_at(key, time.time())
                epoch += 1
                width = max((par for _, par, _ in key), default=0)
                # SKIP THE WIDTHS THIS RUN DOES NOT MEASURE (2026-09-26). The per-width
                # baseline was meant to let the agent learn from the wide step too, and in
                # principle it can — but the driver holds that step for WIDE_HOLD (120 s)
                # while the agent needs warmup+window (420 s), so its window always crosses
                # the next rescale and the epoch is always discarded. Worse, the agent
                # measures epochs SERIALLY: those wasted 420 s delay its detection of the
                # measured epoch until the driver's hold is nearly over, which is why two
                # reps produced one usable episode. Measured on the training run of
                # 2026-09-26: epochs 2 and 4, both at parallelism 3, both discarded.
                measured = bool(measured_widths and width not in measured_widths)
                if measured:
                    print(f"\n[epoca {epoch}] paralelismo {width}, no esta entre los anchos "
                          f"medidos ({sorted(measured_widths)}) — se omite sin gastar ventana",
                          flush=True)
                else:
                    print(f"\n[epoca {epoch}] {time.strftime('%H:%M:%S')} — calentando "
                          f"{args.warmup:.0f}s", flush=True)

            if measured or time.time() - epoch_started < args.warmup:
                time.sleep(args.poll_interval)
                continue
            measured = True

            layout = read_layout(args.node)
            detail = ac.rest(args.rest, f"/jobs/{jid}")
            if not layout or not detail:
                print("  (sin layout de slices o sin detalle del job — epoca descartada)",
                      flush=True)
                continue
            width = len(layout["slices"])
            generator_ids = generator_metric_ids(args.rest, jid, detail)

            disk_ids = disk_metric_ids(args.rest, jid, detail)
            disk_start = disk_bytes(args.rest, jid, detail, disk_ids)
            stall_ids = (disk_metric_ids(args.rest, jid, detail, STALL_SUFFIX)
                         if args.local_credit else {})
            stall_start = disk_bytes(args.rest, jid, detail, stall_ids)
            start_records, start_t = ac.source_records(args.rest, jid), time.time()
            samples = []
            for _ in range(max(1, args.samples)):
                samples.append(subtask_loads(args.rest, jid, detail, generator_ids))
                time.sleep(args.window / max(1, args.samples))
            end_records, end_t = ac.source_records(args.rest, jid), time.time()

            if ac.epoch_key(args.rest, jid) != current_key \
                    or None in (start_records, end_records) or end_records < start_records:
                print("  (la ventana cruzo un reescalado — epoca descartada)", flush=True)
                continue

            rps = (end_records - start_records) / max(1e-6, end_t - start_t)
            loads = {k: statistics.median(s[k] for s in samples if k in s)
                     for k in samples[-1]}
            if args.profile_by == "operator":
                loads = operator_profile(loads)
            per_slice = slice_loads(layout, loads, args.saturation_cut)
            disk_end = disk_bytes(args.rest, jid, detail, disk_ids)
            if args.profile_by == "operator":
                elapsed = max(1e-6, end_t - start_t)
                member_rates = operator_profile(
                    {m: (disk_end[m] - disk_start[m]) / elapsed for m in disk_end
                     if m in disk_start and disk_end[m] >= disk_start[m]})
                per_slice_disk = [sum(member_rates.get(m, 0.0) for m in members)
                                  for members in layout["slices"]]
            else:
                per_slice_disk = slice_disk_rates(layout, disk_start, disk_end, end_t - start_t)
            stall_end = disk_bytes(args.rest, jid, detail, stall_ids)
            # Fraction of the window each TASK spent with RocksDB refusing writes, keyed by task
            # rather than by slice index: the trajectory being credited was decided in an
            # earlier epoch, whose slice numbering need not match this one.
            stall_frac = {t: max(0.0, stall_end[t] - stall_start[t]) / 1e6
                             / max(1e-6, end_t - start_t)
                          for t in stall_end if t in stall_start}

            base = baselines.get(width)
            reward = 0.0 if base is None else rps / base - 1.0
            baselines[width] = rps if base is None else (
                args.baseline_decay * base + (1 - args.baseline_decay) * rps)

            # Latency enters the same way as throughput: relative to a running mean kept per
            # width, so the term is dimensionless and a rescale does not look like a
            # regression. Off by default (weight 0) — turning it on changes what the agent
            # optimises, which is a decision to declare, not a default to inherit.
            delay = sink_delay_ms(args.rest, jid, detail) if args.latency_weight else None
            if delay is not None:
                dbase = delay_baselines.get(width)
                if dbase:
                    reward -= args.latency_weight * (delay / dbase - 1.0)
                delay_baselines[width] = delay if dbase is None else (
                    args.baseline_decay * dbase + (1 - args.baseline_decay) * delay)

            # Machines ranked fastest first: the action is a RANK, not a name, so a table
            # learned here is not tied to this cluster's resource ids.
            speeds = read_speeds(args.node)
            ranked = sorted(layout["tms"], key=lambda t: (-speeds.get(t[0], 1.0), t[0]))
            capacity = [free for _id, free in ranked]

            epsilon = 0.0 if args.freeze else max(
                args.epsilon_min, args.epsilon * args.epsilon_decay ** epoch)
            plan, decisions = decide(agent, per_slice, capacity, epsilon, args.busy_cut,
                                     disk=per_slice_disk)
            # (state, action, the tasks that decision placed) — the tasks are what make it
            # possible to find this decision's own outcome in a later epoch.
            trajectory = [(st, ac_, frozenset(layout["slices"][idx])) for st, ac_, idx in decisions]
            if plan is None:
                print("  ! no alcanzan los slots para el plan — epoca descartada", flush=True)
                continue

            # Credit the trajectory that was in effect for THIS width: a plan published after
            # the epoch began was not the one the fork applied.
            waiting = pending.get(width)
            credited = waiting is not None and waiting[0] < epoch_started and base is not None
            # Naming the reason costs one line and saved a night of guessing: "(sin credito)"
            # alone is indistinguishable between a first observation, a lost plan and a plan
            # published too late.
            if credited:
                why = ""
            elif base is None:
                why = ", primera observacion de este ancho"
            elif waiting is None:
                why = ", sin plan pendiente (se perdio o no se publico)"
            else:
                why = ", el plan se publico despues de empezar la epoca"
            if credited and not args.freeze:
                previous = waiting[1]
                for i, (state, action, members) in enumerate(previous):
                    nxt = trajectory[i] if i < len(trajectory) else (None, None, None)
                    # CREDIT PER DECISION (2026-09-29). Every decision used to receive the same
                    # scalar, so a bad placement rode on the good ones beside it. Here each one
                    # also pays for what happened to ITS slice: the share of the window its
                    # tasks spent with RocksDB stalled. That depends on where the slice landed —
                    # the join stalls on a disk-capped machine and not elsewhere — and a slice
                    # with no state stalls never, so it is not blamed for a jam it did not cause.
                    # A practical difference reward: an approximation of each decision's own
                    # contribution, not a counterfactual.
                    local = min(1.0, sum(stall_frac.get(t, 0.0) for t in members))
                    agent.update(state, action, reward - args.local_credit * local,
                                 nxt[0], nxt[1])
                agent.save()

            chosen = [ranked[plan[i]][0] for i in range(width)]
            plans[width] = {f"{vid}:{sub}": chosen[i]
                            for i, members in enumerate(layout["slices"])
                            for vid, sub in members}
            # KEEP WHAT WAS PLACED, NOT ONLY WHERE (2026-09-17). The first evaluation beat LPT by
            # 23.6% with a fixed plan, and it could not be said which STAGE had gone to which
            # machine: slice composition lives only on the node and is overwritten every
            # decision. Slice indices alone are not enough — the group order changes between
            # jobs — so the layout is saved next to the plan it was decided against.
            names = {v["id"]: v.get("name", "") for v in detail.get("vertices", [])}
            with (out / f"placement-epoch{epoch:03d}.txt").open("w") as trace:
                for i, members in enumerate(layout["slices"]):
                    stages = sorted({names.get(vid, vid[:8]) for vid, _ in members})
                    cost, saturated = per_slice[i]
                    trace.write(f"slice {i} -> {chosen[i]}  load={cost:.1f}"
                                f"  disco={per_slice_disk[i] / 1e6:.1f}MB/s"
                                f"{'  SATURADO' if saturated else ''}"
                                f"  {', '.join(stages)}\n")
            print(f"  publicando plan para anchos {sorted(plans)}...", flush=True)
            ok, error = publish_plan(plans, args.node)
            if ok:
                pending[width] = (time.time(), trajectory)
            else:
                print(f"  ! no se pudo publicar el plan — {error}", flush=True)
                pending.pop(width, None)

            applied = ", ".join(f"{i}->{chosen[i]}" for i in range(min(width, 12)))
            print(f"  slices={width} rps={rps:.0f} base={baselines[width]:.0f} "
                  f"reward={reward:+.3f} {'(acreditada)' if credited else '(sin credito' + why + ')'} "
                  f"eps={epsilon:.2f}", flush=True)
            writer.writerow({"epoch": epoch, "time": time.strftime("%H:%M:%S"),
                             "slices": width, "rps": round(rps, 1),
                             "baseline": round(baselines[width], 1),
                             "e2e_ms": "" if delay is None else round(delay, 1),
                             "e2e_base": ("" if width not in delay_baselines
                                          else round(delay_baselines[width], 1)),
                             "reward": round(reward, 4), "credited": credited,
                             "epsilon": round(epsilon, 3),
                             "plan": " ".join(f"{s}:{a}" for s, a, _ in trajectory),
                             "applied": applied})
            handle.flush()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
