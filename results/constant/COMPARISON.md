# Comparación de Estrategias — Distribución: constant (CONSTANT)

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
| FCFS | 18630 | 8352.8 ev/s |  | % |  |  ms |  ms |  ms |
| BALANCED | 8787 | 7323.9 ev/s |  | % |  |  ms |  ms |  ms |
| LEAST_LOADED | 10905 | 7177.1 ev/s |  | % |  |  ms |  ms |  ms |
| BANDIT | 12439 | 7634.9 ev/s |  | % |  |  ms |  ms |  ms |
| SARSA | 9016 | 7111.1 ev/s |  | % |  |  ms |  ms |  ms |
| ADAPTIVE | 8840 | 4994.9 ev/s |  | % |  |  ms |  ms |  ms |

## Notas

- **Throughput procesado**: eventos que salieron del Latency Tracker (post CPU-Load).
  Es el trabajo útil real — excluye eventos descartados en la cola del source.
- **Eventos procesados**: total absoluto de eventos que completaron el pipeline.
- **Drop %**: % de eventos generados que no entraron al grafo (cola saturada).
- **Latencia**: p50/p95/p99 del procesamiento (event-time → fin de CPU-load),
  promediada entre subtasks. No incluye delay del window tumbling.

Datos detallados: `results/constant/<STRATEGY>/METRICS-SUMMARY.txt`.

