# Comparación de Estrategias de Scheduling

## Metodología
- **Workload**: Nexmark Benchmark
- **Configuración**: 50,000 eventos/seg, 180 segundos, paralelismo 4
- **TaskManagers**: 3 por experimento
- **Repeticiones**: 1 por estrategia

## Resultados

| Estrategia | Duración | Records Procesados | Throughput | CPU Promedio | Distribución |
|------------|----------|-------------------|------------|--------------|--------------|
| FCFS | 3.02m | 9232000 | 50909/s |  |       3 minikube  |
| BALANCED | 3.01m | 9262500 | 51204/s |  |       3 minikube  |
| LEAST_LOADED | 3.01m | 9263250 | 51242/s |  |       3 minikube  |
| ADAPTIVE | 3.01m | 9262250 | 51229/s |  |       3 minikube  |

## Observaciones

### FCFS:
- Primera llegada, primer servicio
- Puede causar desbalance de carga

### BALANCED:
- Distribución equitativa (round-robin)
- Balance predecible

### LEAST_LOADED:
- Asigna al nodo con menos CPU
- Mejor para cargas altas

### ADAPTIVE:
- Cambia estrategia según CPU del cluster
- Combina ventajas de las 3 estrategias

