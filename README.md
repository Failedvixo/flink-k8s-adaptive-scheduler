# Adaptive Resource Scheduler for Flink on Kubernetes

**Master's Thesis Project** - Adaptive scheduling strategies for Apache Flink TaskManagers in Kubernetes clusters.

## 🎯 Overview

This project implements an **adaptive resource scheduler** that dynamically selects scheduling strategies based on cluster CPU utilization to optimize resource allocation for Apache Flink stream processing workloads.

### Key Features

- ✅ **Real Resource Scheduling**: Assigns Flink TaskManager pods to physical Kubernetes nodes
- 🔄 **Adaptive Strategy Switching**: Changes scheduling algorithm based on cluster load
- 📊 **Multiple Strategies**: FCFS, Least-Loaded, Priority-based, and Balanced
- 📈 **Comprehensive Metrics**: Tracks all scheduling decisions for analysis
- 🎓 **Research-Ready**: Designed for experimental evaluation

## 🚀 Quick Start

### Prerequisites

- **Kubernetes**: Minikube, Kind, or cloud cluster
- **kubectl**: Kubernetes CLI
- **Docker**: Container runtime
- **Maven 3.6+**: Build tool
- **Java 11+**: Runtime environment

### Setup
```bash
# 1. Setup cluster
./scripts/setup-cluster.sh

# 2. View scheduler logs
kubectl logs -f -n kube-system -l app=adaptive-scheduler

# 3. Access Flink UI
kubectl port-forward -n flink svc/flink-jobmanager 8081:8081
```

## 📊 Scheduling Strategies

- **FCFS**: CPU < 40% - First available node
- **Balanced**: CPU 40-80% - Round-robin distribution
- **Least-Loaded**: CPU > 80% - Node with lowest CPU

## 📁 Project Structure
```
flink-k8s-adaptive-scheduler/
├── scheduler/          # Adaptive Scheduler
├── kubernetes/         # K8s Manifests
├── scripts/           # Setup Scripts
└── README.md
```

## 🎓 Academic Context

Master's thesis on "Adaptive Resource Scheduling for Stream Processing Workloads in Kubernetes"

## 👥 Author

Your Name - Master's Thesis 2025

## 📄 License

MIT License
```

---

## 📄 **ARCHIVO 2/17: .gitignore**

**Ubicación**: `.gitignore` (raíz del proyecto)
```
# Maven
target/
pom.xml.tag
pom.xml.releaseBackup
pom.xml.versionsBackup
dependency-reduced-pom.xml

# Java
*.class
*.jar
*.war
*.ear
*.log

# IDE
.idea/
*.iml
.vscode/
.project
.classpath
.settings/

# OS
.DS_Store
Thumbs.db

# Kubernetes
*.kubeconfig

# Results
results/
*.csv
*.json

# Python
__pycache__/
*.py[cod]
.venv/
venv/

# Logs
*.log
logs/