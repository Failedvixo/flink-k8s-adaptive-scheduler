# Comparación Global — Todas las Distribuciones

## Configuración común
| Parámetro | Valor |
|-----------|-------|
| Rate base | 200000 ev/s |
| Duración | 180s |
| Paralelismo | 8 |
| Ventana | 10s |
| CPU load | 1000 iter/evento |
| TaskManagers | 5 |

## Matriz: Throughput procesado (ev/s)

| Estrategia | constant | step | sine |
|------------|---:|---:|---:|
| FCFS | 8352.8 | 4265.8 | 7623.4 |
| BALANCED | 7323.9 | 6491.1 | 7126.5 |
| LEAST_LOADED | 7177.1 | 6274.9 | 7554.6 |
| BANDIT | 7634.9 | 6418.9 | 7682.8 |
| SARSA | 7111.1 | 6189.9 | - |
| ADAPTIVE | 4994.9 | 6520.9 | 7926.5 |

## Matriz: Drop % (eventos descartados por saturación)

| Estrategia | constant | step | sine |
|------------|---:|---:|---:|
| FCFS | % | % | % |
| BALANCED | % | % | % |
| LEAST_LOADED | % | % | % |
| BANDIT | % | % | % |
| SARSA | % | % | - |
| ADAPTIVE | % | % | % |

## Matriz: Latencia p95 (ms)

| Estrategia | constant | step | sine |
|------------|---:|---:|---:|
| FCFS |  |  |  |
| BALANCED |  |  |  |
| LEAST_LOADED |  |  |  |
| BANDIT |  |  |  |
| SARSA |  |  | - |
| ADAPTIVE |  |  |  |

## Matriz: Latencia p99 (ms)

| Estrategia | constant | step | sine |
|------------|---:|---:|---:|
| FCFS |  |  |  |
| BALANCED |  |  |  |
| LEAST_LOADED |  |  |  |
| BANDIT |  |  |  |
| SARSA |  |  | - |
| ADAPTIVE |  |  |  |

## Lectura recomendada

1. **Comparar dentro de cada distribución** (ver `results/<dist>/COMPARISON.md`)
   para ver qué estrategia gana en cada régimen.
2. **Comparar una misma estrategia entre distribuciones** (filas de arriba)
   para ver la robustez de cada estrategia ante variabilidad de carga.
3. **Relación drop% vs throughput procesado**: la estrategia ideal maximiza
   throughput procesado minimizando drop%. Si ambos son altos, el pipeline
   está saturado y la estrategia elige bien qué eventos descartar. Si
   throughput procesado es bajo y drop% es alto, la estrategia está
   distribuyendo mal y saturando un nodo.

Datos raw: `results/all-metrics.csv`.

