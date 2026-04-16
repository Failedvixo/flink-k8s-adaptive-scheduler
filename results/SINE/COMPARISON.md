# Comparación de Estrategias — Distribución: sine (SINE)

## Configuración
| Parámetro | Valor |
|-----------|-------|
| Rate base | 200000 ev/s |
| Duración | 180s |
| Paralelismo | 8 |
| Ventana | 10s |
| CPU load | 1000 iter/evento |
| TaskManagers | 5 |

## Resultados

| Estrategia | Estado | Throughput procesado | Eventos procesados | Drop % | Nodos | Lat p50 | Lat p95 | Lat p99 |
|------------|--------|---------------------:|-------------------:|-------:|------:|--------:|--------:|--------:|
| FCFS | 10584 | 7623.4 ev/s |  | % |  |  ms |  ms |  ms |
| BALANCED | 9902 | 7126.5 ev/s |  | % |  |  ms |  ms |  ms |
| LEAST_LOADED | 10371 | 7554.6 ev/s |  | % |  |  ms |  ms |  ms |
| BANDIT | 9909 | 7682.8 ev/s |  | % |  |  ms |  ms |  ms |
| SARSA | FAILED | - | - | - | - | - | - | - |
| ADAPTIVE | 10824 | 7926.5 ev/s |  | % |  |  ms |  ms |  ms |

## Notas

- **Throughput procesado**: eventos que salieron del Latency Tracker (post CPU-Load).
  Es el trabajo útil real — excluye eventos descartados en la cola del source.
- **Eventos procesados**: total absoluto de eventos que completaron el pipeline.
- **Drop %**: % de eventos generados que no entraron al grafo (cola saturada).
- **Latencia**: p50/p95/p99 del procesamiento (event-time → fin de CPU-load),
  promediada entre subtasks. No incluye delay del window tumbling.

Datos detallados: `results/sine/<STRATEGY>/METRICS-SUMMARY.txt`.

