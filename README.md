# Adaptive Resource Scheduler for Flink on Kubernetes

**Master's Thesis Project** - Adaptive scheduling strategies for Apache Flink TaskManagers in Kubernetes clusters using Nexmark benchmark.

---

## 🎯 Overview

This project implements an **adaptive resource scheduler** that dynamically selects scheduling strategies based on cluster CPU utilization to optimize resource allocation for Apache Flink stream processing workloads.

### Key Features

- ✅ **Real Resource Scheduling**: Assigns Flink TaskManager pods to physical Kubernetes nodes
- 🔄 **Adaptive Strategy Switching**: Changes scheduling algorithm based on cluster load (FCFS → Balanced → Least-Loaded)
- 📊 **Multiple Strategies**: FCFS, Least-Loaded, Priority-based, and Balanced scheduling
- 🧪 **Nexmark Benchmark**: Industry-standard streaming benchmark for testing
- 📈 **Configurable Processing Graph**: Flexible topology with filters, windows, and aggregations
- 🎓 **Research-Ready**: Designed for experimental evaluation and thesis demonstration

---

## 🏗️ Architecture
```
┌──────────────────────────────────────────────────────────┐
│              KUBERNETES CLUSTER (3 nodes)                │
│                                                          │
│  ┌────────────────────────────────────────────────────┐ │
│  │  Adaptive Scheduler (Custom Contribution)          │ │
│  │  • Monitors cluster CPU usage                      │ │
│  │  • Switches strategies dynamically                 │ │
│  │  • Schedules TaskManagers to optimal nodes        │ │
│  └────────────────────────────────────────────────────┘ │
│                    ↓ schedules                           │
│  ┌────────────────────────────────────────────────────┐ │
│  │         Apache Flink Cluster                       │ │
│  │  ┌──────────┐  ┌──────────┐  ┌──────────┐        │ │
│  │  │   TM-1   │  │   TM-2   │  │   TM-3   │        │ │
│  │  │ Node-02  │  │ Node-03  │  │ Node-02  │        │ │
│  │  └──────────┘  └──────────┘  └──────────┘        │ │
│  │         ↓ Processing Nexmark workload             │ │
│  └────────────────────────────────────────────────────┘ │
└──────────────────────────────────────────────────────────┘
```

---

## 📋 Project Structure
```
flink-k8s-adaptive-scheduler/
│
├── scheduler/                           # Adaptive Scheduler Implementation
│   ├── src/main/java/com/thesis/scheduler/
│   │   ├── AdaptiveScheduler.java       # Main scheduler (monitors & schedules)
│   │   ├── strategy/                    # Scheduling strategies
│   │   │   ├── SchedulingStrategy.java  # Strategy interface
│   │   │   ├── FCFSStrategy.java        # First-Come-First-Serve
│   │   │   ├── BalancedStrategy.java    # Round-robin distribution
│   │   │   ├── LeastLoadedStrategy.java # CPU-aware assignment
│   │   │   └── PriorityStrategy.java    # Priority-based scheduling
│   │   ├── metrics/
│   │   │   └── ClusterMetrics.java      # CPU metrics collection
│   │   └── model/
│   │       ├── SchedulingStrategyType.java
│   │       └── SchedulingDecision.java  # Decision logging
│   ├── pom.xml                          # Maven configuration
│   └── Dockerfile                       # Container image
│
├── flink-nexmark-job/                   # Nexmark Benchmark Jobs
│   ├── src/main/java/com/thesis/benchmark/
│   │   ├── NexmarkBenchmarkJob.java     # Basic benchmark job
│   │   ├── ConfigurableGraphJob.java    # Configurable processing graph
│   │   └── GraphConfig.java             # Graph topology configuration
│   ├── pom.xml                          # Maven configuration
│   └── Dockerfile                       # (Optional) Custom Flink image
│
├── kubernetes/                          # Kubernetes Manifests
│   ├── scheduler-manifests.yaml         # Scheduler deployment
│   │   ├── ServiceAccount               # RBAC permissions
│   │   ├── ClusterRole                  # Scheduling permissions
│   │   ├── ClusterRoleBinding           # Role binding
│   │   ├── ConfigMap                    # Scheduler configuration
│   │   └── Deployment                   # Scheduler pod
│   └── flink-manifests.yaml             # Flink cluster deployment
│       ├── Namespace                    # flink namespace
│       ├── ConfigMap                    # Flink configuration
│       ├── JobManager                   # Flink JobManager
│       └── TaskManager                  # Flink TaskManagers (uses adaptive-scheduler)
│
├── scripts/                             # Automation Scripts
│   ├── setup-cluster.sh                 # Complete cluster setup
│   └── clean-all.sh                     # Cleanup all resources
│
├── README.md                            # This file
└── .gitignore                           # Git ignore rules
```

---

## 🚀 Quick Start

### Prerequisites

- **Kubernetes**: Minikube 1.30+ (or Kind/K8s cluster)
- **kubectl**: 1.28+
- **Docker**: 20.10+
- **Maven**: 3.6+
- **Java**: 11+
- **WSL2** (if on Windows)

### Installation
```bash
# 1. Clone repository
git clone <your-repo-url>
cd flink-k8s-adaptive-scheduler

# 2. Make scripts executable
chmod +x scripts/*.sh

# 3. Run complete setup
./scripts/setup-cluster.sh
```

**This will:**
- ✅ Start Minikube with 3 nodes
- ✅ Build and deploy adaptive scheduler
- ✅ Deploy Flink cluster (3 TaskManagers)
- ✅ Verify everything is running

**Expected time**: ~5 minutes

---

## 📊 Scheduling Strategies

The scheduler dynamically switches strategies based on cluster CPU load:

| CPU Load | Strategy | Behavior | Use Case |
|----------|----------|----------|----------|
| **< 40%** | **FCFS** | Assign to first available node | Low load, simple assignment |
| **40-80%** | **Balanced** | Round-robin distribution | Medium load, even distribution |
| **> 80%** | **Least-Loaded** | Assign to node with lowest CPU | High load, avoid hotspots |
| **Any** | **Priority** | High-priority pods to best nodes | Mixed workload priorities |

---

## 🧪 Running Nexmark Benchmark

### Basic Benchmark Job
```bash
# Execute basic Nexmark job
kubectl exec -n flink deployment/flink-jobmanager -- \
  flink run -d /tmp/nexmark-job.jar 50000 300

# Parameters:
# - 50000: events per second
# - 300: duration in seconds (5 minutes)
```

### Configurable Graph Job
```bash
# Execute with custom configuration
kubectl exec -n flink deployment/flink-jobmanager -- \
  flink run -d /tmp/nexmark-config.jar 100000 600 4 30

# Parameters:
# - 100000: events/sec
# - 600: duration (10 min)
# - 4: parallelism
# - 30: window size (seconds)
```

### Modify Graph Topology

Edit `flink-nexmark-job/src/main/java/com/thesis/benchmark/GraphConfig.java`:
```java
// Enable/disable operators
public boolean enableHighValueFilter = true;      // Filter high-value bids
public boolean enableCurrencyConversion = true;   // USD to EUR conversion
public boolean enableAuctionFilter = false;       // Filter by auction ID
public boolean enableBidderFilter = false;        // Filter by bidder ID

// Configure windowing
public WindowType windowType = WindowType.TUMBLING;  // TUMBLING, SLIDING, or SESSION
public int windowSizeSeconds = 10;
public int slideSizeSeconds = 5;                  // For SLIDING windows

// Configure aggregation
public AggregationType aggregationType = AggregationType.SUM;  // SUM, AVERAGE, COUNT, MAX, MIN
```

After modifications:
```bash
cd flink-nexmark-job
mvn clean package
kubectl cp target/flink-nexmark-job-1.0.0.jar \
  flink/<jobmanager-pod>:/tmp/nexmark-config.jar
```

---

## 📈 Monitoring & Verification

### View Scheduler Decisions
```bash
# Real-time logs
kubectl logs -f -n kube-system -l app=adaptive-scheduler

# Recent decisions
kubectl logs -n kube-system -l app=adaptive-scheduler --tail=50 | grep SCHEDULING

# Strategy switches
kubectl logs -n kube-system -l app=adaptive-scheduler | grep "STRATEGY SWITCH"
```

**Expected output:**
```
[SCHEDULING] Pod: flink-taskmanager-abc123
  Strategy: BALANCED
  Result: ✓ SCHEDULED to minikube-m02
  Node CPU: 45.2%
  Total scheduled: 3

[STRATEGY SWITCH]
  From: FCFS
  To: BALANCED
  Reason: Cluster CPU = 62.3%
  Time: 2025-11-20 15:23:45
```

### View TaskManager Placement
```bash
# Check distribution across nodes
kubectl get pods -n flink -l component=taskmanager -o wide

# Verify scheduler usage
kubectl get pod -n flink -l component=taskmanager -o yaml | grep schedulerName
```

### Access Flink UI
```bash
# Port-forward Flink UI
kubectl port-forward -n flink svc/flink-jobmanager 8081:8081
```

Open: **http://localhost:8081**

You'll see:
- Job graph visualization
- TaskManager distribution
- Running jobs
- Metrics and throughput

---

## 🧪 Testing Adaptive Behavior

### Test 1: Observe Strategy Switches
```bash
# Scale TaskManagers to trigger re-scheduling
kubectl scale deployment flink-taskmanager -n flink --replicas=0
sleep 10
kubectl scale deployment flink-taskmanager -n flink --replicas=5

# Watch scheduler adapt
kubectl logs -f -n kube-system -l app=adaptive-scheduler
```

### Test 2: Load-Based Adaptation
```bash
# Low load job (FCFS expected)
kubectl exec -n flink deployment/flink-jobmanager -- \
  flink run -d /tmp/nexmark-job.jar 5000 180

# High load job (LEAST_LOADED expected)
kubectl exec -n flink deployment/flink-jobmanager -- \
  flink run -d /tmp/nexmark-job.jar 100000 180
```

### Test 3: View Jobs
```bash
# List active jobs
kubectl exec -n flink deployment/flink-jobmanager -- flink list

# Cancel job
kubectl exec -n flink deployment/flink-jobmanager -- flink cancel <JOB_ID>
```

---

## 🔧 Configuration

### Scheduler Configuration

Edit `kubernetes/scheduler-manifests.yaml`:
```yaml
data:
  cpu-low-threshold: "40.0"    # Switch from FCFS to BALANCED
  cpu-high-threshold: "80.0"   # Switch from BALANCED to LEAST_LOADED
  strategy-cooldown: "30"      # Seconds between strategy changes
```

Apply changes:
```bash
kubectl apply -f kubernetes/scheduler-manifests.yaml
kubectl rollout restart deployment/adaptive-scheduler -n kube-system
```

### Flink Cluster Configuration

Edit `kubernetes/flink-manifests.yaml`:
```yaml
spec:
  replicas: 3              # Number of TaskManagers
  resources:
    requests:
      cpu: "1000m"
      memory: "1728m"
```

---

## 📸 Collecting Evidence (For Thesis)
```bash
# Create results directory
mkdir -p results

# 1. Scheduler decisions
kubectl logs -n kube-system -l app=adaptive-scheduler > results/scheduler-logs.txt

# 2. Strategy switches only
kubectl logs -n kube-system -l app=adaptive-scheduler | grep "STRATEGY SWITCH" > results/strategy-switches.txt

# 3. TaskManager placement
kubectl get pods -n flink -o wide > results/taskmanager-placement.txt

# 4. Node metrics
kubectl top nodes > results/node-metrics.txt

# 5. Jobs executed
kubectl exec -n flink deployment/flink-jobmanager -- flink list > results/flink-jobs.txt
```

---

## 🧹 Cleanup
```bash
# Remove all resources
./scripts/clean-all.sh

# Or manually:
kubectl delete namespace flink
kubectl delete deployment adaptive-scheduler -n kube-system
kubectl delete clusterrolebinding adaptive-scheduler
kubectl delete clusterrole adaptive-scheduler
minikube delete  # Optional: delete entire cluster
```

---

## 🎓 Thesis Contribution

### What This Project Demonstrates

1. **Custom Kubernetes Scheduler**: Full implementation of a production-ready scheduler
2. **Adaptive Resource Management**: Dynamic strategy selection based on real-time metrics
3. **Real Workload Testing**: Integration with Nexmark industry-standard benchmark
4. **Observable Behavior**: Complete logging and metrics for analysis
5. **Reproducible Results**: Automated setup and configuration

### Experimental Capabilities

- Compare scheduling strategies under different loads
- Measure impact on stream processing performance
- Analyze resource utilization patterns
- Demonstrate cost-efficiency improvements

### Research Questions Addressed

1. How does adaptive scheduling affect resource utilization?
2. What is the optimal strategy for different workload patterns?
3. How does scheduling impact stream processing latency and throughput?

---

## 🐛 Troubleshooting

### Scheduler Not Scheduling
```bash
# Check scheduler is running
kubectl get pods -n kube-system -l app=adaptive-scheduler

# Check logs for errors
kubectl logs -n kube-system -l app=adaptive-scheduler --tail=100

# Verify permissions
kubectl auth can-i create pods/binding --as=system:serviceaccount:kube-system:adaptive-scheduler
```

### TaskManagers Pending
```bash
# Check if scheduler name is correct
kubectl get pod -n flink <pod-name> -o yaml | grep schedulerName

# Should show: schedulerName: adaptive-scheduler
# If not, reapply manifests:
kubectl apply -f kubernetes/flink-manifests.yaml
```

### Build Failures
```bash
# Clean and rebuild
cd scheduler
mvn clean package -DskipTests

cd ../flink-nexmark-job
mvn clean package -DskipTests
```

---

## 📚 Key Files Explained

### Core Components

| File | Purpose | Lines | Key Functions |
|------|---------|-------|---------------|
| `AdaptiveScheduler.java` | Main scheduler logic | ~400 | `schedulePod()`, `checkAndSwitchStrategy()` |
| `FCFSStrategy.java` | FCFS implementation | ~30 | `selectNode()` |
| `BalancedStrategy.java` | Round-robin | ~35 | `selectNode()` with counter |
| `LeastLoadedStrategy.java` | CPU-aware scheduling | ~35 | `selectNode()` with CPU check |
| `ClusterMetrics.java` | Metrics collection | ~150 | `getNodeCpuUsage()`, `getAverageClusterCpuUsage()` |
| `GraphConfig.java` | Nexmark configuration | ~140 | Configuration flags and enums |
| `ConfigurableGraphJob.java` | Nexmark job | ~250 | `buildSource()`, `applyFilters()`, `applyWindows()` |

---

## 📧 Contact

**Author**: [Your Name]  
**Thesis**: Master's in Computer Science 2025  
**Institution**: [Your University]  
**Email**: your.email@university.edu

---

## 📄 License

MIT License - See LICENSE file for details

---

## 🙏 Acknowledgments

- Apache Flink community
- Kubernetes SIG Scheduling
- Nexmark benchmark contributors
- Thesis advisor: [Professor Name]

---

**Status**: ✅ Complete & Working Project - Ready for Experiments & Thesis Defense

**Last Updated**: November 2025