---
name: project-communication-closed
description: "El término de comunicación de SP-Ant: implementado, verificado que cambia las decisiones, y medido nulo dos veces. Cerrado con mecanismo, no con duda."
metadata:
  type: project
---

**La línea de comunicación (SP-Ant, Ec. 5) está CERRADA con evidencia doble.**

**Campaña 1** (20260829-*, un solo nodo, sin peso de comunicación): barrido de `PAYLOAD_BYTES`
0/512/2048 entre LPT y ACO. Nulo: p = 0.39 / 0.34 / 0.75, sin tendencia.

**Campaña 2** (20260831-173722, el intento fuerte): los tres TaskManagers en **tres nodos de
Kubernetes distintos**, `THESIS_COST_COMMUNICATION=1.0`, `PAYLOAD_BYTES=2048`, brazos OPTIMAL/ACO/LPT.
Todo verificado aplicado (nombre del job con `pay=2048B`, pods en `minikube`/`m02`/`m03`, pesos en el
env del JM).

- **El mecanismo SÍ funciona**: OPTIMAL y ACO agrupan los slices distinto que LPT y coinciden entre
  ellos en qué mantener junto, 12 de 12 repeticiones. El objetivo cambia la decisión.
- **Sin efecto medible**: ACO −0.2% (p=0.97), OPTIMAL −4.1% (p=0.58) frente a LPT.

**Por qué**: los tres nodos de minikube son contenedores sobre el mismo host — los tres reportan
12 núcleos y 8 GB, que son los del portátil. Cruzar de nodo pasa por un bridge, no por un cable. A
2048 B × ~15k reg/s son ~30 MB/s, nada. **Repartir entre nodos NO aísla CPU tampoco**, por lo mismo.

**Lo que sí quedó demostrado, y sirve para el punto de RL**: LPT es **estructuralmente incapaz** de
optimizar un objetivo de dos términos — es un greedy sobre un escalar y no tiene mecanismo para
negociar makespan contra tráfico. Es un argumento a favor de métodos de búsqueda que NO depende del
tamaño del espacio.

**Para revivirlo hace falta infraestructura, no diseño**: máquinas reales sobre una red real.

Relacionado: [[project_paper_spant]], [[project_headline_result]].
