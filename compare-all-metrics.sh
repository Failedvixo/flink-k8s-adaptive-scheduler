#!/bin/bash

echo "=========================================="
echo "  COMPARACIÓN DE TODAS LAS MÉTRICAS"
echo "=========================================="

mkdir -p results/high-load

cat > results/high-load/METRICS_COMPARISON.md << 'COMP'
# Comparación de Estrategias - Alta Carga (200k evt/s)

## Resumen Ejecutivo

| Estrategia | Estado | Throughput | Duración | Distribución | Balance |
|------------|--------|------------|----------|--------------|---------|
COMP

for STRATEGY in FCFS BALANCED LEAST_LOADED ADAPTIVE; do
  DIR="results/high-load/$STRATEGY"
  
  if [ -f "$DIR/job-details.json" ]; then
    # Estado
    STATE=$(jq -r '.state' "$DIR/job-details.json" 2>/dev/null || echo "N/A")
    
    # Duración
    DURATION_MS=$(jq -r '.duration' "$DIR/job-details.json" 2>/dev/null || echo "0")
    DURATION_SEC=$(echo "scale=1; $DURATION_MS / 1000" | bc 2>/dev/null || echo "N/A")
    
    # Throughput
    TOTAL_RECORDS=$(jq -r '.vertices[0]."metrics"."write-records" // 0' "$DIR/job-details.json" 2>/dev/null)
    if [ "$TOTAL_RECORDS" != "0" ] && [ "$TOTAL_RECORDS" != "null" ] && [ "$DURATION_MS" != "0" ]; then
      THROUGHPUT=$(echo "scale=0; $TOTAL_RECORDS * 1000 / $DURATION_MS" | bc 2>/dev/null || echo "N/A")
      THROUGHPUT="${THROUGHPUT} evt/s"
    else
      THROUGHPUT="N/A"
    fi
    
    # Distribución
    if [ -f "$DIR/taskmanager-placement.txt" ]; then
      DIST=$(awk 'NR>1 {print $7}' "$DIR/taskmanager-placement.txt" | sort | uniq -c | tr '\n' ' ')
      NODES=$(awk 'NR>1 {print $7}' "$DIR/taskmanager-placement.txt" | sort -u | wc -l)
      
      if [ "$NODES" -eq 1 ]; then
        BALANCE="❌ Desbalanceado"
      elif [ "$NODES" -eq 3 ]; then
        BALANCE="✅ Balanceado"
      else
        BALANCE="⚠️ Parcial"
      fi
    else
      DIST="N/A"
      BALANCE="N/A"
    fi
    
    echo "| **$STRATEGY** | $STATE | $THROUGHPUT | ${DURATION_SEC}s | $DIST | $BALANCE |" >> results/high-load/METRICS_COMPARISON.md
  else
    echo "| **$STRATEGY** | No ejecutado | - | - | - | - |" >> results/high-load/METRICS_COMPARISON.md
  fi
done

cat >> results/high-load/METRICS_COMPARISON.md << 'FOOTER'

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

FOOTER

# Agregar observaciones específicas
for STRATEGY in FCFS BALANCED LEAST_LOADED ADAPTIVE; do
  DIR="results/high-load/$STRATEGY"
  
  if [ -f "$DIR/SUMMARY.txt" ]; then
    echo "" >> results/high-load/METRICS_COMPARISON.md
    echo "### Resultados de $STRATEGY:" >> results/high-load/METRICS_COMPARISON.md
    
    # Estado del job
    STATE=$(grep "Estado final:" "$DIR/SUMMARY.txt" | cut -d: -f2)
    echo "- Estado: $STATE" >> results/high-load/METRICS_COMPARISON.md
    
    # Distribución
    if [ -f "$DIR/taskmanager-placement.txt" ]; then
      NODES=$(awk 'NR>1 {print $7}' "$DIR/taskmanager-placement.txt" | sort -u | wc -l)
      echo "- TaskManagers en $NODES nodo(s)" >> results/high-load/METRICS_COMPARISON.md
    fi
    
    # Scheduling decisions
    DECISIONS=$(grep "Scheduling decisions:" "$DIR/SUMMARY.txt" | cut -d: -f2 | tr -d ' ')
    echo "- Decisiones de scheduling: $DECISIONS" >> results/high-load/METRICS_COMPARISON.md
    
    # Strategy switches (solo para ADAPTIVE)
    if [ "$STRATEGY" = "ADAPTIVE" ]; then
      SWITCHES=$(grep "Strategy switches:" "$DIR/SUMMARY.txt" 2>/dev/null | cut -d: -f2 | tr -d ' ')
      if [ -n "$SWITCHES" ]; then
        echo "- Cambios de estrategia: $SWITCHES" >> results/high-load/METRICS_COMPARISON.md
      fi
    fi
  fi
done

cat >> results/high-load/METRICS_COMPARISON.md << 'CONCLUSION'

## Conclusión

El scheduler **ADAPTIVE** combina las ventajas de las tres estrategias fijas:
- Usa **FCFS** cuando la carga es baja (eficiente)
- Usa **BALANCED** cuando la carga es media (predecible)
- Usa **LEAST_LOADED** cuando la carga es alta (óptimo)

Esto permite adaptarse dinámicamente a diferentes condiciones de carga sin intervención manual.

CONCLUSION

echo ""
echo "=========================================="
echo "  COMPARACIÓN GENERADA"
echo "=========================================="
echo ""
cat results/high-load/METRICS_COMPARISON.md
echo ""
echo "Archivo guardado en: results/high-load/METRICS_COMPARISON.md"
