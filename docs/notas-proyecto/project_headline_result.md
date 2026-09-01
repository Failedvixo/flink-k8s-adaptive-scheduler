---
name: project-headline-result
description: "El resultado citable: LPT +48% y ACO +63% sobre Flink estándar, orden balanceado, ambos bajo Bonferroni en throughput y latencia. Incluye CÓMO analizar (estratificar por slices, throughput absoluto)."
metadata:
  type: project
---

**Las dos campañas del 2026-08-30 son el resultado citable**, agrupadas:
`20260830-141910` (STOCK primero) y `20260830-204709` (ACO primero, STOCK último).
PER_STAGE, SUBMIT_PAR=3 / TARGET_PAR=2 / PIN_PARALLELISM=5, REPS=12, clúster 4/2/1 núcleos con
2/4/6 slots.

**Estratificado en `slices=7`, sobre `source_out_rps` (absoluto), permutación 10k:**

| brazo | n | reg/s | Δ vs STOCK | p |
|---|---|---|---|---|
| STOCK | 33 | 9308 | — | — |
| LEAST_LOADED | 26 | 8469 | −9.0% | 0.46 |
| LPT | 26 | 13789 | **+48.1%** | **0.0001** |
| ACO | 29 | 15176 | **+63.0%** | **0.0001** |

Latencia e2e: LPT −18.6% (p=0.0025), ACO −20.3% (p=0.0005). Todo bajo Bonferroni (α=0.0083).
**ACO vs LPT: +10.1%, p=0.117 — indistinguibles.**

**EL CONTROL QUE DESCARTA LA DERIVA.** Los brazos corren en bloques secuenciales, así que una deriva
del sistema quedaría confundida con el brazo. Se corrió la misma campaña con el orden invertido: con
STOCK al final —la posición supuestamente favorecida— STOCK EMPEORÓ (7682 contra 11036). El efecto
es del brazo, no de la posición.

**CÓMO ANALIZAR, y no es opcional.** Los episodios vienen de dos puntos de operación (`slices=7`, el
reescalado medido, y `slices=10`, la restauración) y los brazos reciben mezclas distintas. Hay que
**estratificar por `slices` y usar `source_out_rps`**, nunca `throughput_per_slot` agrupado — esa
combinación produjo un "+33% p=0.0012" que era artefacto del análisis.

**LO QUE NO SE PUDO DEMOSTRAR.** La campaña `20260831-144914` (con `PIN_VERTEX="Latency Tracker:1"`
para desigualar las cargas de los slices) sí logró que los brazos eligieran emplazamientos distintos
—OPTIMAL y ACO ponen dos slices en `fast`, LPT solo uno— pero el throughput medido NO los separó:
CV del 40% y n=13 solo detectan diferencias sobre el 31%. Underpowered, no nulo.

**El brazo OPTIMAL** (`ExhaustivePlacement`) enumera las 1148 asignaciones y devuelve el mínimo del
costo. Es el techo DEL MODELO, no de la realidad: si no gana en throughput medido, eso es evidencia
contra la función de costo, no contra el enumerador.

**EL RUIDO ES HAMBRUNA DE LA FUENTE, NO VARIABILIDAD INTRÍNSECA (hallazgo 2026-08-31).**
`r(backpressure_mean_ms_s, throughput) = +0.694`: los episodios de bajo throughput tienen
contrapresión BAJA (176-200 contra 294-300 en los buenos). No estaban congestionados — estaban
**hambrientos**: el generador no alcanzó los 60k/s porque compite por CPU con los operadores que se
miden. `recovery_s` no correlaciona (r=+0.08), así que no son recuperaciones lentas.

**Filtrar a `backpressure_mean_ms_s >= 280` baja el CV de LPT de 18% a 11%** y el umbral detectable
de 12% a 9%. Y cambia una conclusión:

| | sin filtro | filtrado |
|---|---|---|
| LPT vs STOCK | +48.1% (p=0.0001) | +34.0% (p=0.0007) |
| ACO vs STOCK | +63.0% (p=0.0001) | +34.7% (p=0.0025) |
| **ACO vs LPT** | +10.1% (p=0.12) | **+0.5% (p=0.92)** |

El aparente +10% de ACO sobre LPT lo producían los episodios hambrientos. Bien alimentados son
**idénticos dentro de 0.5%**. El filtro es CONSERVADOR contra la conclusión: sube a STOCK de 9308 a
10781 porque sus peores emplazamientos son los que causan hambruna, y aun así pierde por 34%.

**Reportar siempre las dos versiones.** Y el arreglo de fondo del testbed es dar CPU propia al
generador, o bajar la tasa objetivo para que no sea él el cuello de botella.

**EL PISO DE MEDICIÓN, medido el 2026-08-31 — es la restricción dominante del proyecto.**
Diferencia mínima detectable entre dos brazos con n=26: **12%** (sd de LPT 2508 sobre 13789, CV 18%).
Mayor brecha del modelo que se logró construir sobre cargas reales: **4.4%** (Q8 a paralelismo 2, 10
slices). En el sintético actual y en Q8 a paralelismo 1 la brecha de LPT contra el óptimo enumerado
es **0.0%**.

Factor de tres entre lo que el modelo ofrece y lo que el montaje ve. Por eso STOCK vs LPT SÍ se mide
(+48%, brecha enorme entre "sin criterio" y "consciente de capacidad") y LPT vs ACO vs OPTIMAL NO.

**Subir repeticiones no lo arregla**: el umbral cae con √n, así que bajar de 4.4% pide ~9× las
repeticiones, unas 18 h de clúster. Lo que hay que hacer es construir instancias con brecha de
modelo > 12-15%. La receta, de simulación: **dos slices pesados desiguales entre sí por ≥25% y
grandes respecto del ideal divisible**. Los intentos del 31-08 fallaron porque quedaron a 10% de
diferencia (1074.7 vs 972.4) y LPT los resolvió óptimamente.

**Perfil de Q8 medido** (por si sirve): watermarks 204, filter-persons 126.5, filter-auctions 111,
new-users-join **10.5**, resto 0. El join es casi gratis porque Nexmark genera ~92% pujas y Q8 une
personas con subastas, ambas raras; lo caro son los filtros que descartan el 92%.

Relacionado: [[project_rl_goal_operator_profiling]], [[project_freeslots_submission]].
