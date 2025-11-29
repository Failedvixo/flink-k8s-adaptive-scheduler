# Comparación de Estrategias - Alta Carga (200k evt/s)

| Estrategia | Estado | Duración | Distribución | Switches |
|------------|--------|----------|--------------|----------|
| FCFS | FINISHED | 180segundos
181463ms |       5 minikube |  |
| BALANCED | FINISHED | 180segundos
181470ms |       2 minikube |  |
| LEAST_LOADED | FINISHED | 180segundos
181547ms |       4 minikube-m02 |  |
| ADAPTIVE | FINISHED | 180segundos
181302ms |       5 minikube |  |
