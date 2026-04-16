# Comparación de Estrategias — Distribución: step (STEP)

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
| FCFS | 7518 | 4265.8 ev/s |  | % |  |  ms |  ms |  ms |
| BALANCED | 9676 | 6491.1 ev/s |  | % |  |  ms |  ms |  ms |
| LEAST_LOADED | 10147 | 6274.9 ev/s |  | % |  |  ms |  ms |  ms |
| BANDIT | 9801 | 6418.9 ev/s |  | % |  |  ms |  ms |  ms |
| SARSA | 9688 | 6189.9 ev/s |  | % |  |  ms |  ms |  ms |
| ADAPTIVE | 10287 | 6520.9 ev/s |  | % |  |  ms |  ms |  ms |

## Notas

- **Throughput procesado**: eventos que salieron del Latency Tracker (post CPU-Load).
  Es el trabajo útil real — excluye eventos descartados en la cola del source.
- **Eventos procesados**: total absoluto de eventos que completaron el pipeline.
- **Drop %**: % de eventos generados que no entraron al grafo (cola saturada).
- **Latencia**: p50/p95/p99 del procesamiento (event-time → fin de CPU-load),
  promediada entre subtasks. No incluye delay del window tumbling.

Datos detallados: `results/step/<STRATEGY>/METRICS-SUMMARY.txt`.

