#!/usr/bin/env python3
"""Genera el diagrama de pipeline de SARSA_META para la presentación.

Salida: results/plots/sarsa_meta_pipeline.png
"""
from pathlib import Path
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch, FancyArrowPatch

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "results/plots/sarsa_meta_pipeline.png"
OUT.parent.mkdir(parents=True, exist_ok=True)

C = {
    "raw":   "#dbeafe",  # azul claro  - métricas crudas
    "feat":  "#bfdbfe",  # azul        - features
    "state": "#bbf7d0",  # verde       - estado
    "reward":"#fde68a",  # amarillo    - recompensa
    "train": "#fca5a5",  # rojo        - entrenamiento SARSA
    "qtab":  "#c4b5fd",  # violeta     - tabla Q
    "run":   "#a7f3d0",  # verde agua  - runtime
}
EDGE = "#334155"

fig, ax = plt.subplots(figsize=(15, 9))
ax.set_xlim(0, 100)
ax.set_ylim(0, 100)
ax.axis("off")

def box(x, y, w, h, text, color, fs=10, bold_first=True):
    ax.add_patch(FancyBboxPatch(
        (x, y), w, h, boxstyle="round,pad=0.6,rounding_size=2",
        linewidth=1.6, edgecolor=EDGE, facecolor=color, zorder=2))
    if bold_first:
        lines = text.split("\n")
        ax.text(x + w/2, y + h - 4.0, lines[0], ha="center", va="top",
                fontsize=fs+1, fontweight="bold", zorder=3)
        if len(lines) > 1:
            ax.text(x + w/2, y + h - 9.0, "\n".join(lines[1:]), ha="center",
                    va="top", fontsize=fs, zorder=3, linespacing=1.35)
    else:
        ax.text(x + w/2, y + h/2, text, ha="center", va="center",
                fontsize=fs, zorder=3, linespacing=1.35)

def arrow(x1, y1, x2, y2, text=None, color=EDGE, style="-|>"):
    ax.add_patch(FancyArrowPatch(
        (x1, y1), (x2, y2), arrowstyle=style, mutation_scale=20,
        linewidth=1.8, color=color, zorder=1,
        connectionstyle="arc3,rad=0"))
    if text:
        ax.text((x1+x2)/2, (y1+y2)/2 + 2.2, text, ha="center", va="bottom",
                fontsize=9, style="italic", color=color, zorder=4)

ax.text(50, 97, "SARSA_META — pipeline de datos", ha="center",
        fontsize=17, fontweight="bold")

# ---------- Fila de ENTRENAMIENTO (offline) ----------
ax.text(2, 90, "ENTRENAMIENTO  (offline, corridas q2-*)", ha="left",
        fontsize=12, fontweight="bold", color="#b91c1c")

box(1,  72, 20, 14,
    "Métricas crudas\nautoscaler.log (~10s)\n• CPU% por nodo\n• mem% por nodo\n• busy_inst", C["raw"], fs=9)
box(26, 72, 21, 14,
    "Vector de contexto\n13 features\navg/max/min cpu,\nimbalances, velocities,\nsaturation, mem, busy", C["feat"], fs=9)
box(52, 72, 21, 14,
    "Estado discreto\n3 features de estado:\nsaturation, cpu_imbalance,\nmem_imbalance\nterciles 3×3×3", C["state"], fs=9)
box(78, 72, 21, 14,
    "Recompensa R\nMETRICS-SUMMARY.txt\nThroughput / core\nnormalizado → [0,1]", C["reward"], fs=9)

arrow(21, 79, 26, 79)
arrow(47, 79, 52, 79)

# SARSA + tabla Q (centro)
box(26, 47, 32, 16,
    "SARSA  TD(0)  offline\nQ(s,a) ← Q(s,a) + α·[ R + γ·Q(s',a) − Q(s,a) ]\n"
    "a' = a   (el brazo es fijo dentro de la corrida)\n→ evaluación de política por brazo",
    C["train"], fs=10)
box(64, 47, 31, 16,
    "Tabla Q  (matriz estados × brazos)\n18 estados  ×  4 brazos\n"
    "ej.  '2,2,2' → BAL 8.6 | FCFS 5.0 | LL 4.5 | BAN 0\n"
    "π(s) = argmax_a Q(s,a)", C["qtab"], fs=9)

arrow(62, 72, 46, 63, "estado s")           # estado -> SARSA
arrow(88, 72, 50, 63, "recompensa R")        # reward -> SARSA
arrow(58, 55, 64, 55)                        # SARSA -> Qtable

# ---------- Fila de EJECUCIÓN (runtime) ----------
ax.text(2, 38, "EJECUCIÓN  (runtime, cada 30s — solo lee la tabla)", ha="left",
        fontsize=12, fontweight="bold", color="#047857")

box(1,  18, 24, 14,
    "Contexto en vivo\nmétricas del clúster →\nmismo vector 13 feat →\ndiscretiza estado s\n(mismos bordes)", C["run"], fs=9)
box(31, 18, 28, 14,
    "Decisión meta\nπ(s) = argmax_a Q(s,a)\nestado no visto →\nbrazo por defecto (BALANCED)", C["run"], fs=9)
box(65, 18, 30, 14,
    "Delegación\nselectNode() en el\nbrazo base elegido\n(FCFS/BAL/LL/BANDIT)\n→ coloca el pod TaskManager", C["run"], fs=9)

arrow(25, 25, 31, 25)
arrow(59, 25, 65, 25)
arrow(79, 47, 79, 32, "consulta\ntabla Q", color="#7c3aed")  # Qtable -> runtime decision

# leyenda
ax.text(50, 5, "Entrenado en q2-{const,sine,step} · evaluado zero-shot en q5/q8 · "
        "la tabla Q se entrena offline y solo se consulta en runtime (sin cold start)",
        ha="center", fontsize=9.5, style="italic", color="#475569")

plt.tight_layout()
plt.savefig(OUT, dpi=150, bbox_inches="tight", facecolor="white")
print(f"Wrote {OUT.relative_to(ROOT)}")
