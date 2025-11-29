#!/bin/bash

echo "=========================================="
echo "  COMPARACIÓN DE RESULTADOS"
echo "=========================================="

cat > results/high-load/COMPARISON.md << 'COMP'
# Comparación de Estrategias - Alta Carga (200k evt/s)

| Estrategia | Estado | Duración | Distribución | Switches |
|------------|--------|----------|--------------|----------|
COMP

for STRATEGY in FCFS BALANCED LEAST_LOADED ADAPTIVE; do
  DIR="results/high-load/$STRATEGY"
  
  if [ -f "$DIR/SUMMARY.txt" ]; then
    STATE=$(grep "Estado final:" "$DIR/SUMMARY.txt" | cut -d: -f2 | tr -d ' ')
    DURATION=$(grep "Duración:" "$DIR/SUMMARY.txt" | cut -d: -f2 | tr -d ' ')
    DIST=$(grep -A1 "Distribución de TaskManagers:" "$DIR/SUMMARY.txt" | tail -1)
    SWITCHES=$(grep "Strategy switches:" "$DIR/SUMMARY.txt" | cut -d: -f2 | tr -d ' ' || echo "N/A")
    
    echo "| $STRATEGY | $STATE | $DURATION | $DIST | $SWITCHES |" >> results/high-load/COMPARISON.md
  fi
done

cat results/high-load/COMPARISON.md

