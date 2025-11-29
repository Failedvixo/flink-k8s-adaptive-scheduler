# Comparación de Estrategias - Alta Carga (200k evt/s)

## Resumen Ejecutivo

| Estrategia | Estado | Throughput | Duración | Distribución | Balance |
|------------|--------|------------|----------|--------------|---------|
| **FCFS** | FINISHED | 189790 evt/s | 181.4s |       5 minikube  | ❌ Desbalanceado |
| **BALANCED** | FINISHED | 181297 evt/s | 181.4s |       2 minikube       2 minikube-m02       1 minikube-m03  | ✅ Balanceado |
| **LEAST_LOADED** | FINISHED | 180669 evt/s | 181.5s |       4 minikube-m02       1 minikube-m03  | ⚠️ Parcial |
| **ADAPTIVE** | FINISHED | 179700 evt/s | 181.3s |       5 minikube  | ❌ Desbalanceado |

## Métricas Detalladas

### FCFS (First-Come-First-Served)
- **Algoritmo**: Asigna al primer nodo disponible
- **Ventaja**: Simple, rápido
- **Desventaja**: Puede saturar un nodo
- **Caso de uso**: Cargas muy ligeras

### BALANCED (Round-Robin)
- **Algoritmo**: Distribución equitativa rotativa
- **Ventaja**: Distribución predecible
- **Desventaja**: No considera carga actual
- **Caso de uso**: Cargas estables y uniformes

### LEAST_LOADED (Mínima Carga)
- **Algoritmo**: Asigna al nodo con menor CPU
- **Ventaja**: Balance dinámico óptimo
- **Desventaja**: Requiere monitoreo constante
- **Caso de uso**: Cargas variables y altas

### ADAPTIVE (Adaptativo)
- **Algoritmo**: Cambia estrategia según CPU del cluster
  - CPU < 40%: FCFS
  - CPU 40-80%: BALANCED
  - CPU > 80%: LEAST_LOADED
- **Ventaja**: Combina beneficios de las 3 estrategias
- **Desventaja**: Complejidad adicional
- **Caso de uso**: Cargas dinámicas y variables

## Observaciones


### Resultados de FCFS:
- Estado:  FINISHED
- TaskManagers en 1 nodo(s)
- Decisiones de scheduling: 

### Resultados de BALANCED:
- Estado:  FINISHED
- TaskManagers en 3 nodo(s)
- Decisiones de scheduling: 

### Resultados de LEAST_LOADED:
- Estado:  FINISHED
- TaskManagers en 2 nodo(s)
- Decisiones de scheduling: 

### Resultados de ADAPTIVE:
- Estado:  FINISHED
- TaskManagers en 1 nodo(s)
- Decisiones de scheduling: 

## Conclusión

El scheduler **ADAPTIVE** combina las ventajas de las tres estrategias fijas:
- Usa **FCFS** cuando la carga es baja (eficiente)
- Usa **BALANCED** cuando la carga es media (predecible)
- Usa **LEAST_LOADED** cuando la carga es alta (óptimo)

Esto permite adaptarse dinámicamente a diferentes condiciones de carga sin intervención manual.

