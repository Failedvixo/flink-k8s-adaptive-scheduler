#!/bin/bash
# ============================================
# Cap one TaskManager's disk bandwidth: the second resource dimension
# ============================================
#
# WHY (2026-09-28). The bench had one real dimension of heterogeneity — CPU quota — because
# the three TaskManagers are containers on one host sharing one disk. With one dimension the
# placement problem collapses into "order by demand, fill fastest first", and that is what the
# trained agent converged to: every row of its table picked the fastest machine with room.
#
# The second dimension that CETSA (Li et al., IEEE TBD 2023) models is MEMORY, and for Q8 it
# does not bind: tripling the RocksDB cache moved the join's miss rate from 30.9% to 27.5%
# and left bytes read from disk unchanged, because a tumbling-window join scans each window
# once and never reuses a block. What the join does do is WRITE — 2.2 GB per subtask, about
# 9.5 MB/s at 40000 rec/s, while every other operator writes nothing. So the binding second
# resource for a stateful join is state I/O, and cgroup v2's io.max lets each TaskManager have
# its own budget of it on the shared device, which is the only way to make disk a property of
# a MACHINE rather than of the host.
#
# EMULATION, stated plainly: io.max caps bandwidth, it does not give a separate device with its
# own queue, so latency does not behave exactly like a second SSD. The effect being modelled
# is bandwidth contention, which is exactly what it caps.
#
# NOT persistent: the cap lives in the pod's cgroup, and a restarted pod gets a new one. The
# placement driver restarts TaskManagers before every arm, so it reapplies the cap through
# DISK_LIMIT after each restart rather than relying on this having been run once.
#
# Usage:
#   scripts/limit-disk.sh fast 10M       # cap reads and writes of the fast TaskManager
#   scripts/limit-disk.sh fast off       # remove the cap
#   scripts/limit-disk.sh --read         # what each TaskManager has now

set -u

NS=flink
NODE="${THESIS_NODE:-minikube}"
CLASSES="fast medium slow"

node_sh() {
    timeout -k 5 30 docker exec "$NODE" sh -c "$1" </dev/null 2>/dev/null
}

# The device that backs the pods' filesystems, found rather than assumed: /var inside the node.
DEVICE=$(node_sh 'd=$(df /var | tail -1 | cut -d" " -f1); cat /sys/block/$(basename "$d")/dev 2>/dev/null')
[ -n "$DEVICE" ] || { echo "ERROR: no encuentro el dispositivo de /var en el nodo" >&2; exit 1; }

to_bytes() {
    case "$1" in
        off|max) echo max ;;
        *[Kk])   echo $(( ${1%[Kk]} * 1024 )) ;;
        *[Mm])   echo $(( ${1%[Mm]} * 1024 * 1024 )) ;;
        *[Gg])   echo $(( ${1%[Gg]} * 1024 * 1024 * 1024 )) ;;
        *)       echo "$1" ;;
    esac
}

cgroup_of() {
    local uid
    uid=$(kubectl get pod -n "$NS" -l "component=taskmanager,speed-class=$1" \
        --field-selector=status.phase=Running -o jsonpath='{.items[0].metadata.uid}' 2>/dev/null)
    [ -n "$uid" ] || return 1
    # systemd writes the pod UID with underscores in the slice name.
    node_sh "find /sys/fs/cgroup/kubepods.slice -maxdepth 3 -type d -name '*pod${uid//-/_}.slice' | head -1"
}

if [ "${1:-}" = "--read" ]; then
    echo "dispositivo: $DEVICE"
    for c in $CLASSES; do
        cg=$(cgroup_of "$c") || { echo "  $c: sin pod"; continue; }
        lim=$(node_sh "grep '^$DEVICE ' $cg/io.max")
        echo "  $c: ${lim:-sin límite}"
    done
    exit 0
fi

CLASS="${1:-}"; BW="${2:-}"
case "$CLASS" in fast|medium|slow) ;; *) echo "Usage: $0 fast|medium|slow <10M|off>  |  $0 --read" >&2; exit 2 ;; esac
[ -n "$BW" ] || { echo "falta el ancho de banda (p.ej. 10M) u 'off'" >&2; exit 2; }

CG=$(cgroup_of "$CLASS")
[ -n "$CG" ] || { echo "ERROR: no encuentro el cgroup del TaskManager $CLASS" >&2; exit 1; }
BYTES=$(to_bytes "$BW")
node_sh "echo '$DEVICE rbps=$BYTES wbps=$BYTES' > $CG/io.max" \
    || { echo "ERROR: no pude escribir $CG/io.max" >&2; exit 1; }

NOW=$(node_sh "grep '^$DEVICE ' $CG/io.max")
if [ "$BYTES" = max ]; then
    echo "$CLASS: límite de disco retirado"
else
    echo "$CLASS: disco limitado a $BW/s en lectura y escritura  ($NOW)"
fi
