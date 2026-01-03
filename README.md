# Adaptive Resource Scheduler for Flink on Kubernetes

**Master's Thesis Project** - Adaptive scheduling strategies for Apache Flink TaskManagers in Kubernetes clusters using Nexmark benchmark with **real Kubernetes Metrics Server data**.

---

## 🎯 Overview

This project implements an **adaptive resource scheduler** that dynamically selects scheduling strategies based on **real cluster CPU utilization** (from Kubernetes Metrics Server) to optimize resource allocation for Apache Flink stream processing workloads.

### Key Features

- ✅ **Real Resource Scheduling**: Assigns Flink TaskManager pods to physical Kubernetes nodes
- ✅ **Real Metrics**: Uses Kubernetes Metrics Server API for actual CPU measurements
- 🔄 **Adaptive Strategy Switching**: Changes scheduling algorithm based on cluster load
- 🎰 **Multi-Armed Bandit (UCB1)**: Reinforcement learning-based scheduling strategy
- 📊 **5 Scheduling Strategies**: FCFS, Least-Loaded, Balanced, Priority, and Bandit
- 🧪 **Nexmark Benchmark**: Industry-standard streaming benchmark for testing
- 📈 **Configurable Processing Graph**: Flexible topology with filters, windows, and aggregations

---

## 📊 Experimental Results

Results from experiments with Nexmark benchmark (50k events/s, 120s duration, 5 TaskManagers, 3 nodes):

| Strategy | Throughput (evt/s) | Distribución | Nodos Usados | Backpressure |
|----------|-------------------|--------------|--------------|--------------|
| **FCFS** | 40,885 | 5-0-0 | 1 | 0ms |
| **LEAST_LOADED** | 40,804 | 0-0-5 | 1 | 0ms |
| **BALANCED** | 40,911 | 2-2-1 | 3 | 0ms |
| **BANDIT (UCB1)** | 47,469 | 2-2-1 | 3 | 0ms |
| **ADAPTIVE** | 45,277 | 5-0-0 | 1 | 2,211ms |

### Key Findings

- **BANDIT** achieves best throughput (+16% vs FCFS) with optimal distribution
- **BALANCED** and **BANDIT** distribute pods across all 3 nodes
- **FCFS** and **LEAST_LOADED** concentrate all pods in a single node

---

## 🏗️ Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│                 KUBERNETES CLUSTER (3 nodes)                      │
│                                                                    │
│  ┌──────────────────────────────────────────────────────────────┐ │
│  │           Adaptive Scheduler (kube-system namespace)          │ │
│  │  ┌─────────────────────────────────────────────────────────┐ │ │
│  │  │  ClusterMetrics.java                                     │ │ │
│  │  │  • Connects to Metrics Server API                        │ │ │
│  │  │  • Gets REAL CPU usage per node                          │ │ │
│  │  └─────────────────────────────────────────────────────────┘ │ │
│  │                          ↓                                    │ │
│  │  ┌─────────────────────────────────────────────────────────┐ │ │
│  │  │  AdaptiveScheduler.java                                  │ │ │
│  │  │  • Monitors cluster CPU                                  │ │ │
│  │  │  • Switches strategies: FCFS → LEAST_LOADED → BANDIT     │ │ │
│  │  │  • Binds pods to optimal nodes                           │ │ │
│  │  └─────────────────────────────────────────────────────────┘ │ │
│  │                          ↓                                    │ │
│  │  ┌─────────────────────────────────────────────────────────┐ │ │
│  │  │  Scheduling Strategies                                   │ │ │
│  │  │  • FCFSStrategy.java        (First available node)       │ │ │
│  │  │  • LeastLoadedStrategy.java (Lowest CPU node)            │ │ │
│  │  │  • BalancedStrategy.java    (Round-robin)                │ │ │
│  │  │  • PriorityStrategy.java    (Priority-based)             │ │ │
│  │  │  • BanditStrategy.java      (UCB1 algorithm) ⭐ NEW       │ │ │
│  │  └─────────────────────────────────────────────────────────┘ │ │
│  └──────────────────────────────────────────────────────────────┘ │
│                              ↓ schedules                          │
│  ┌──────────────────────────────────────────────────────────────┐ │
│  │              Apache Flink Cluster (flink namespace)           │ │
│  │                                                                │ │
│  │   minikube          minikube-m02        minikube-m03          │ │
│  │  ┌─────────┐       ┌─────────┐         ┌─────────┐            │ │
│  │  │  TM-1   │       │  TM-3   │         │  TM-5   │            │ │
│  │  │  TM-2   │       │  TM-4   │         │JobMngr  │            │ │
│  │  └─────────┘       └─────────┘         └─────────┘            │ │
│  │                                                                │ │
│  │         Processing Nexmark Benchmark Workload                  │ │
│  └──────────────────────────────────────────────────────────────┘ │
└──────────────────────────────────────────────────────────────────┘
```

---

## 📋 Project Structure

```
flink-k8s-adaptive-scheduler/
│
├── scheduler/                              # Adaptive Scheduler Implementation
│   ├── src/main/java/com/thesis/scheduler/
│   │   ├── AdaptiveScheduler.java          # Main scheduler with strategy switching
│   │   ├── strategy/
│   │   │   ├── SchedulingStrategy.java     # Strategy interface
│   │   │   ├── FCFSStrategy.java           # First-Come-First-Serve
│   │   │   ├── BalancedStrategy.java       # Round-robin distribution
│   │   │   ├── LeastLoadedStrategy.java    # CPU-aware assignment
│   │   │   ├── PriorityStrategy.java       # Priority-based scheduling
│   │   │   └── BanditStrategy.java         # ⭐ Multi-Armed Bandit (UCB1)
│   │   ├── metrics/
│   │   │   └── ClusterMetrics.java         # Real metrics from Metrics Server
│   │   └── model/
│   │       ├── SchedulingStrategyType.java # Strategy enum
│   │       └── SchedulingDecision.java     # Decision logging
│   ├── pom.xml                             # Maven dependencies
│   └── Dockerfile                          # Container image
│
├── flink-nexmark-job/                      # Nexmark Benchmark Jobs
│   ├── src/main/java/com/thesis/benchmark/
│   │   ├── ConfigurableGraphJob.java       # Configurable processing graph
│   │   └── GraphConfig.java                # Graph topology configuration
│   └── pom.xml                             # Maven dependencies
│
├── kubernetes/                             # Kubernetes Manifests
│   ├── scheduler-manifests.yaml            # Scheduler deployment + RBAC
│   └── flink-manifests.yaml                # Flink cluster deployment
│
├── results/                                # Experiment Results
│   └── high-load/
│       ├── FCFS/
│       ├── LEAST_LOADED/
│       ├── BALANCED/
│       ├── BANDIT/
│       └── ADAPTIVE/
│
├── extract-metrics.sh                      # Metrics extraction script
└── README.md                               # This file
```

---

## 🔧 Prerequisites & Dependencies

### System Requirements

| Component | Version | Purpose |
|-----------|---------|---------|
| **Minikube** | 1.30+ | Local Kubernetes cluster |
| **kubectl** | 1.28+ | Kubernetes CLI |
| **Docker** | 20.10+ | Container runtime |
| **Maven** | 3.6+ | Java build tool |
| **Java JDK** | 11 | Runtime environment |
| **WSL2** | Latest | Windows Subsystem for Linux |

### Installation Commands (Ubuntu/WSL2)

```bash
# Java 11
sudo apt update
sudo apt install openjdk-11-jdk -y
java -version

# Maven
sudo apt install maven -y
mvn -version

# Docker
sudo apt install docker.io -y
sudo usermod -aG docker $USER

# kubectl
curl -LO "https://dl.k8s.io/release/$(curl -L -s https://dl.k8s.io/release/stable.txt)/bin/linux/amd64/kubectl"
sudo install -o root -g root -m 0755 kubectl /usr/local/bin/kubectl
kubectl version --client

# Minikube
curl -LO https://storage.googleapis.com/minikube/releases/latest/minikube-linux-amd64
sudo install minikube-linux-amd64 /usr/local/bin/minikube
minikube version
```

### Maven Dependencies (Scheduler)

```xml
<!-- pom.xml - Key Dependencies -->
<dependencies>
    <!-- Kubernetes Java Client -->
    <dependency>
        <groupId>io.kubernetes</groupId>
        <artifactId>client-java</artifactId>
        <version>18.0.1</version>
    </dependency>
    <dependency>
        <groupId>io.kubernetes</groupId>
        <artifactId>client-java-extended</artifactId>
        <version>18.0.1</version>
    </dependency>
    
    <!-- Logging -->
    <dependency>
        <groupId>org.slf4j</groupId>
        <artifactId>slf4j-api</artifactId>
        <version>1.7.36</version>
    </dependency>
    <dependency>
        <groupId>ch.qos.logback</groupId>
        <artifactId>logback-classic</artifactId>
        <version>1.2.11</version>
    </dependency>
</dependencies>
```

### Maven Dependencies (Flink Nexmark Job)

```xml
<!-- pom.xml - Key Dependencies -->
<dependencies>
    <!-- Apache Flink -->
    <dependency>
        <groupId>org.apache.flink</groupId>
        <artifactId>flink-streaming-java</artifactId>
        <version>1.18.0</version>
    </dependency>
    <dependency>
        <groupId>org.apache.flink</groupId>
        <artifactId>flink-clients</artifactId>
        <version>1.18.0</version>
    </dependency>
</dependencies>
```

---

## 🚀 Quick Start

### 1. Start Minikube Cluster (3 nodes)

```bash
minikube start --nodes 3 --cpus 2 --memory 4096

# Enable Metrics Server (REQUIRED for real metrics)
minikube addons enable metrics-server

# Verify nodes
kubectl get nodes
```

### 2. Build and Deploy Scheduler

```bash
cd scheduler

# Compile
mvn clean package -DskipTests

# Build Docker image
docker build -t adaptive-scheduler:latest . --no-cache

# Load into Minikube
minikube image load adaptive-scheduler:latest

# Deploy
kubectl apply -f ../kubernetes/scheduler-manifests.yaml

# Verify
kubectl get pods -n kube-system -l app=adaptive-scheduler
kubectl logs -n kube-system -l app=adaptive-scheduler --tail=30
```

### 3. Deploy Flink Cluster

```bash
# Deploy Flink
kubectl apply -f kubernetes/flink-manifests.yaml

# Wait for pods
kubectl wait --for=condition=ready pod -l component=taskmanager -n flink --timeout=120s

# Verify
kubectl get pods -n flink -o wide
```

### 4. Build and Deploy Nexmark Job

```bash
cd flink-nexmark-job

# Compile
mvn clean package -DskipTests

# Copy JAR to JobManager
kubectl cp target/flink-nexmark-job-1.0.0.jar \
  flink/$(kubectl get pod -n flink -l component=jobmanager -o jsonpath='{.items[0].metadata.name}'):/tmp/nexmark.jar

# Verify
kubectl exec -n flink deployment/flink-jobmanager -- ls -la /tmp/nexmark.jar
```

### 5. Run Benchmark

```bash
kubectl exec -n flink deployment/flink-jobmanager -- \
  flink run -d /tmp/nexmark.jar 50000 120 4 10

# Parameters: events/sec, duration, parallelism, window_size
```

---

## 📊 Scheduling Strategies

### Adaptive Strategy Selection

```java
private SchedulingStrategyType selectStrategyForCpu(double cpuUsage) {
    if (cpuUsage > 60.0) {
        return SchedulingStrategyType.BANDIT;       // High load: use learning
    } else if (cpuUsage > 30.0) {
        return SchedulingStrategyType.LEAST_LOADED; // Medium load: balance
    } else {
        return SchedulingStrategyType.FCFS;         // Low load: simple
    }
}
```

| CPU Load | Strategy | Behavior |
|----------|----------|----------|
| **0-30%** | FCFS | First available node (simple, fast) |
| **30-60%** | LEAST_LOADED | Node with lowest CPU |
| **>60%** | BANDIT | UCB1 algorithm learns optimal distribution |

### Multi-Armed Bandit (UCB1) Algorithm

The BANDIT strategy uses reinforcement learning to optimize node selection:

```
UCB(node) = Q(node) + c × √(ln(N) / n(node))
            ────────   ─────────────────────
            Exploitation    Exploration
```

Where:
- **Q(node)**: Average reward for this node
- **N**: Total selections across all nodes
- **n(node)**: Times this node was selected
- **c**: Exploration parameter (√2)

**Reward Function:**
```
CPU 30-70%  → reward = 1.0   (optimal)
CPU < 30%   → reward = 0.5-0.8 (under-utilized)
CPU 70-90%  → reward = 0.5-1.0 (high but ok)
CPU > 90%   → reward = 0.2   (overloaded)
```

---

## 🧪 Running Experiments

### Test Individual Strategy

```bash
# 1. Edit AdaptiveScheduler.java
#    Change: this.currentStrategy = SchedulingStrategyType.FCFS;
#    Change: return SchedulingStrategyType.FCFS;

# 2. Build and deploy
cd scheduler
mvn clean package -DskipTests
docker build -t adaptive-scheduler:fcfs . --no-cache
minikube image load adaptive-scheduler:fcfs
kubectl set image deployment/adaptive-scheduler -n kube-system scheduler=adaptive-scheduler:fcfs

# 3. Verify strategy
kubectl logs -n kube-system -l app=adaptive-scheduler --tail=20 | grep "Initial Strategy"

# 4. Reset TaskManagers
kubectl scale deployment flink-taskmanager -n flink --replicas=0
sleep 15
kubectl scale deployment flink-taskmanager -n flink --replicas=5
kubectl wait --for=condition=ready pod -l component=taskmanager -n flink --timeout=120s

# 5. Run job
kubectl exec -n flink deployment/flink-jobmanager -- \
  flink run -d /tmp/nexmark.jar 50000 120 4 10

# 6. Wait and extract metrics
sleep 140
./extract-metrics.sh FCFS
```

### Compare All Strategies

Repeat the above for each strategy:
- `SchedulingStrategyType.FCFS`
- `SchedulingStrategyType.LEAST_LOADED`
- `SchedulingStrategyType.BALANCED`
- `SchedulingStrategyType.BANDIT`

---

## 📈 Monitoring

### View Scheduler Logs

```bash
# Real-time logs
kubectl logs -f -n kube-system -l app=adaptive-scheduler

# Check strategy
kubectl logs -n kube-system -l app=adaptive-scheduler --tail=20 | grep -E "Initial Strategy|Strategy:"

# View scheduling decisions
kubectl logs -n kube-system -l app=adaptive-scheduler --tail=50 | grep "SCHEDULING"

# View strategy switches
kubectl logs -n kube-system -l app=adaptive-scheduler | grep "STRATEGY SWITCH"
```

### View Pod Distribution

```bash
kubectl get pods -n flink -o wide
```

### View Node Metrics

```bash
kubectl top nodes
```

### Access Flink UI

```bash
kubectl port-forward -n flink svc/flink-jobmanager 8081:8081
# Open: http://localhost:8081
```

---

## 📁 Results Structure

After running `./extract-metrics.sh STRATEGY_NAME`:

```
results/high-load/STRATEGY_NAME/
├── job-details.json          # Full job information from Flink API
├── taskmanager-placement.txt # Pod distribution across nodes
├── node-metrics.txt          # CPU/Memory per node
├── pod-metrics.txt           # CPU/Memory per pod
├── scheduler-logs.txt        # Scheduler decision logs
├── backpressure-summary.txt  # Backpressure per operator
├── METRICS-SUMMARY.txt       # Human-readable summary
└── metrics.csv               # CSV for analysis
```

---

## 🐛 Troubleshooting

### Metrics Server Not Available

```bash
# Enable metrics server
minikube addons enable metrics-server

# Wait and verify
sleep 60
kubectl top nodes
```

### Scheduler 403 Forbidden Error

```bash
# Reapply RBAC permissions
kubectl delete clusterrolebinding adaptive-scheduler
kubectl apply -f kubernetes/scheduler-manifests.yaml
```

### JAR Not Found in JobManager

```bash
# Re-copy the JAR
kubectl cp flink-nexmark-job/target/flink-nexmark-job-1.0.0.jar \
  flink/$(kubectl get pod -n flink -l component=jobmanager -o jsonpath='{.items[0].metadata.name}'):/tmp/nexmark.jar
```

### Minikube Connection Refused

```bash
minikube status
minikube start --nodes 3
```

---

## 🎓 Thesis Contribution

### Research Questions Addressed

1. **How does adaptive scheduling affect resource utilization?**
   - BANDIT achieves 16% better throughput than FCFS
   
2. **What is the optimal strategy for different workload patterns?**
   - Low load: FCFS (simple, no overhead)
   - High load: BANDIT (learns optimal distribution)

3. **Can reinforcement learning improve scheduling decisions?**
   - Yes, UCB1 learns to distribute pods across nodes effectively

### Key Innovations

1. **Real Metrics Integration**: Uses Kubernetes Metrics Server API instead of simulated data
2. **Multi-Armed Bandit Scheduling**: Novel application of UCB1 to pod scheduling
3. **Adaptive Strategy Switching**: Dynamic selection based on cluster state

---

## 📧 Contact

**Author**: Vicente  
**Thesis**: Master's in Computer Science 2025  
**Project**: Adaptive Resource Scheduler for Stream Processing

---

## 📄 License

MIT License - See LICENSE file for details

---

**Status**: ✅ Complete & Working - Ready for Experiments

**Last Updated**: December 2025