# Comparación de Estrategias de Scheduling

## Metodología
- **Workload**: Nexmark Benchmark
- **Configuración**: 50,000 eventos/seg, 180 segundos, paralelismo 4
- **TaskManagers**: 3 por experimento
- **Repeticiones**: 1 por estrategia

## Resultados

| Estrategia | Duración | Records Procesados | Throughput | CPU Promedio | Distribución |
|------------|----------|-------------------|------------|--------------|--------------|
| FCFS | 3.01m | 9195000 | 50867/s |  |       1 minikube       1 minikube-m02       1 minikube-m03  |
| BALANCED | 3.01m | 9180000 | 50761/s |  |       1 minikube       1 minikube-m02       1 minikube-m03  |
| LEAST_LOADED | 3.01m | 9210000 | 50967/s |  |       2 minikube-m02       1 minikube-m03  |
| ADAPTIVE | 3.01m | 9210000 | 50935/s |  |       1 minikube       1 minikube-m02       1 minikube-m03  |

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

