---
name: project-cost-model-does-not-predict
description: "El hallazgo del 2026-09-01: la función de costo (makespan sobre cargas medidas) NO predice el throughput. Medido con el brazo OPTIMAL contra el óptimo enumerado."
metadata:
  type: project
---

**Campaña `20260901-115717`.** Instancia diseñada para maximizar la brecha del modelo: SHARED,
`PIN_VERTEX="Source:6,CPU Load:2,Latency Tracker:6,Window:1,Sink:1"`, `SUBMIT_PAR=12 TARGET_PAR=6`,
6 slices con cargas `[1069, 950, 217, 217, 217, 217]`. Los cuatro brazos eligieron emplazamientos
**genuinamente distintos** por primera vez.

| brazo | reg/s | Δ vs STOCK | p |
|---|---|---|---|
| STOCK | 16056 | — | — |
| LPT | 16601 | +3.4% | 0.55 |
| ACO | 16850 | +4.9% | 0.40 |
| OPTIMAL | 16864 | +5.0% | 0.34 |

**OPTIMAL vs LPT medido: +1.6% (p=0.67). El modelo predecía 15.6%.**

**LA CONCLUSIÓN: la función de costo no predice el throughput.** El makespan suma las cargas de cada
máquina y divide por su velocidad, como si los co-inquilinos se repartieran la capacidad en
proporción a lo que piden. Pero **un slot reserva memoria, no CPU**: un co-inquilino ocioso deja sus
núcleos libres al que trabaja. Lo que gobierna el throughput es cuántos núcleos alcanza efectivamente
la subtarea cuello de botella, no la carga sumada de la máquina.

Con los números: OPTIMAL pone los dos slices pesados en `fast` → 2 núcleos efectivos cada uno. LPT
pone uno en `fast` con un relleno ocioso (≈4 núcleos) y otro en `medium` con otro relleno ocioso
(≈2 núcleos). **Mismo cuello de botella, mismo throughput.**

Predice algo falsable: un emplazamiento que le dé más núcleos efectivos al cuello de botella debería
ganar, y **ningún brazo optimiza eso**.

**SEGUNDA EXPLICACIÓN VIVA, no separable con este experimento.** Fijar el operador de CPU en
paralelismo 2 mientras el resto corre en 6 lo volvió cuello de botella dominante: `busy=111`,
`backpressure=500` (contra 250 y 238 en la campaña del titular). Con las subtareas bloqueadas la
mitad del tiempo, el emplazamiento de 4 de los 6 slices era irrelevante.

**REGLA DE DISEÑO QUE SALE DE AHÍ:** mantener ocupaciones comparables entre etapas. Si un vértice
domina 10× al resto, la campaña no puede medir nada. Verificable en la calibración antes de gastar
dos horas.

**Y UN ERROR METODOLÓGICO A NO REPETIR:** la instancia se eligió maximizando la brecha contra el
MODELO (18.9% entre 7776 configuraciones). Como el modelo no predice el throughput, maximizar su
brecha no maximiza la brecha medible. Se optimizó la métrica equivocada.

Cómo se reconcilia con [[project_headline_result]]: **las decisiones gruesas importan (34-48% entre
tener criterio de capacidad y no tenerlo) y las finas no (1.6% entre el greedy y el óptimo exacto)**.

Relacionado: [[project_headline_result]], [[project_rl_goal_operator_profiling]].
