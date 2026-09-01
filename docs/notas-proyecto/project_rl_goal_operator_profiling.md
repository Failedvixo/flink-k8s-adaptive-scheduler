---
name: project-rl-goal-operator-profiling
description: "El objetivo de la tesis es un RL que CARACTERICE los operadores y decida qué operador va en qué máquina — no un RL que elija entre brazos ni que aprenda a co-localizar."
metadata:
  type: project
---

**La contribución que Vicente quiere es una estrategia de RL que caracterice los vértices del grafo
de procesamiento y decida, a partir de esa caracterización, qué operador va en qué TaskManager.**
Reafirmado el 2026-08-28 tras varias campañas: "la idea es una estrategia de rl que pueda
caracterizar los operadores y elegir cual es mejor a cual máquina, si este escenario no es bueno
para probar esto hay que hacer uno que sí lo sea".

**Qué NO es** (los tres desvíos en que ya cayó el trabajo):
- No es el meta-scheduler SARSA que elige entre brazos — eso opera un nivel más arriba.
- No es aprender a co-localizar operadores comunicativos — eso es SP-Ant, es relacional (depende de
  la pareja, no del vértice), y se resuelve con un término de comunicación sin aprender nada.
- No es re-derivar LPT. Sobre una carga ESCALAR, LPT es exacto en instancias de este tamaño
  (verificado: makespan 567 es el piso y LPT llega). Un RL sobre un escalar solo puede empatarle.

**La condición necesaria para que el objetivo sea probable:** las máquinas deben diferir en MÁS DE
UNA dimensión de recurso y los operadores deben diferir en CUÁL dimensión los limita. Entonces la
carga deja de ser un escalar, LPT no tiene un orden que aplicar, y "qué necesita este operador" pasa
a ser un problema de representación — que es lo que un agente puede aprender y una heurística no.
Caso testigo: un operador con `busyTimeMsPerSecond` BAJO puede necesitar la máquina de memoria
grande; un modelo escalar lo llama "liviano" y lo manda a la peor máquina.

**Estado al 2026-08-28:** el clúster solo difiere en CPU (4/2/1 núcleos). Mientras eso siga así,
cualquier RL de emplazamiento va a re-aprender LPT. Falta: una clase de máquina de perfil cruzado
(pocos núcleos, mucha memoria), un operador acotado por estado y no por CPU, y un brazo RL que lea
features por vértice (busy%, tamaño de estado, records in/out, backpressure) en vez de un escalar.

Relacionado: [[project_phase3_reward_design]], [[project_paper_spant]], [[project_freeslots_submission]].
