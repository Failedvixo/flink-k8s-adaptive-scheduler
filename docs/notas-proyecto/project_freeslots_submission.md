---
name: project-freeslots-submission
description: "El asignador solo ve los slots que el job pidió al enviarse, no la capacidad del clúster — una máquina puede estar sana y registrada y aun así quedar fuera de la decisión."
metadata:
  type: project
---

**`freeSlots` en `thesis-assign.log` es siempre el requisito de slots del job AL ENVIARSE, nunca
la capacidad del clúster.** El JobManager declara su requisito al ResourceManager, este le ofrece
los TaskManagers que hagan falta para cubrirlo, y el asignador decide únicamente sobre ese
conjunto. Un TM sano, registrado y con slots libres queda fuera de la decisión si el job nunca
pidió tanto.

Confirmado en tres corridas (28-08-2026):

| corrida | SUBMIT_PAR | modo | pide | freeSlots | tmsAvailable |
|---|---|---|---|---|---|
| 20260828-145014 | 12 | SHARED | 12 | 12 | 3 |
| 20260826-182540 | 10 | SHARED | 10 | 10 | 2 |
| 20260828-172500 | 3 | PER_STAGE | 10 | 10 | 2 |

En el clúster heterogéneo `slow`(6 slots) + `medium`(4) suman exactamente 10, así que **cualquier
job que pida 10 o menos deja fuera a `tm-3-fast`** y la campaña compara solo dos clases de máquina
sin avisar. Fue la causa real de que la campaña 182540 no tocara la máquina rápida (el pod muerto
por NodeAffinity fue una segunda causa que coincidió).

**Why:** invalida silenciosamente el contraste que la campaña existe para medir. `run.json` registra
las tres clases y las velocidades publicadas, así que el diseño parece correcto en el papel.

**How to apply:** el requisito de slots debe superar 10 para que `fast` entre en el pool. El job lo
imprime al arrancar (`Slots required:`), y `GraphConfig.slotsRequired()` lo calcula sin levantar
nada. Verificar SIEMPRE `tmsAvailable=3` en `thesis-assign.log` antes de analizar resultados —
`assert_taskmanagers_registered` en `run-placement-experiment.sh` comprueba el REGISTRO, que es
necesario pero no suficiente.

Relacionado: [[project_flink_2x_migration]], [[feedback_pitfalls]].
