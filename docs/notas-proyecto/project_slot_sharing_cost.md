---
name: project-slot-sharing-cost
description: "Cuánto cuesta romper el slot sharing: nada en throughput, 3.5x en slots. Medido el 2026-08-31 con LPT a paralelismo fijo."
metadata:
  type: project
---

**Campañas pareadas `20260831-202037` (SHARED) y `20260831-204929` (PER_STAGE)**, corridas seguidas
para compartir condiciones del host, mismo brazo (LPT), mismo `CPU_LOAD=800`, y **el mismo vector de
cargas** (B con `PUBLISH_LOADS=0` reutilizó el que midió A). Paso medido a paralelismo 2 en ambas.

| modo | slices | reg/s | e2e (ms) |
|---|---|---|---|
| SHARED | 2 | 33179 | 10025 |
| PER_STAGE | 7 | 31325 | 9606 |

**PER_STAGE vs SHARED: −5.6% de throughput, IC95% [−13%, +3%], p=0.25.** No significativo.

**La conclusión: el precio de romper el slot sharing NO es rendimiento, es CAPACIDAD.** Mismo trabajo
y throughput indistinguible, pero **3.5× los slots** (7 contra 2). Por eso obliga a paralelismo bajo
en un clúster chico, y por eso las instancias salen fáciles. En un clúster con slots de sobra sería
casi gratis.

**ADVERTENCIA: `CPU_LOAD=800`, no 2500.** Estas dos campañas NO son comparables con las del titular.

**Por qué hubo que bajar la carga.** Con `CPU_LOAD=2500`, SHARED estrechando a paralelismo 2 es
inestable: el job entra en `RESTARTING` entre reescalados y el harness corta el brazo (falló en rep 8
y en rep 3 en dos intentos). El pipeline entero de 2 subtareas para 60k ev/s a 2500 iteraciones no se
sostiene. Con 800 aguanta las 10 repeticiones. Configuraciones probadas estables bajo SHARED: `12/6`.

Relacionado: [[project_headline_result]], [[project_rl_goal_operator_profiling]].
