# KubeAI-Sentry

A multi-tenant Kubernetes simulation that models AI workload lifecycles — Inference, Training, and Data Cleansing — using CPU and RAM as proxies for GPU and VRAM. Demonstrates three core cluster management properties: **resource isolation**, **OOMKill detection**, and **priority-based scheduling**.

---
![kubeai_diagram](https://github.com/user-attachments/assets/b1fa0a9c-d736-4e64-b218-4780f05ccfab)


## How It Works

Three mock workloads run as Kubernetes Deployments across two isolated tenants:

| Workload | Tenant | Behavior |
|---|---|---|
| `mock-inference` | `tenant-alpha` | Steady 40–60% CPU, moderate RAM |
| `mock-training` | `tenant-beta` | Burst 80–95% CPU, high RAM, periodic spikes |
| `mock-data-cleansing` | `tenant-beta` | Light CPU, moderate RAM, simulated I/O wait |

Each workload reads env vars (`LOAD_PROFILE`, `MEMORY_TARGET_MB`, `CPU_CORES`, `DURATION_SECONDS`) so behavior is controlled entirely by the recipe YAML — no image rebuilds needed.

The `training-noisy` recipe deliberately sets `MEMORY_TARGET_MB` above the container's memory limit, causing the kernel to OOMKill the pod. The profiler detects this in real time via `containerStatuses[].lastState.terminated.reason`.

---

## Project Structure

```
kubeai-sentry/
├── docker/                     Mock workload images (Python 3.11-slim, stdlib only)
│   ├── mock-inference/
│   ├── mock-training/
│   └── mock-data-cleansing/
├── recipes/                    WorkloadRecipe YAMLs (custom schema)
│   ├── inference-standard.yaml   High-priority inference in tenant-alpha
│   ├── training-heavy.yaml       Burst training in tenant-beta
│   ├── training-noisy.yaml       OOMKill trigger (700MB in 512Mi limit)
│   └── data-cleansing.yaml       Light pipeline workload
├── k8s/                        Kubernetes manifests
│   ├── namespaces/               tenant-alpha and tenant-beta
│   ├── quotas/                   ResourceQuota + LimitRange per tenant
│   └── priority-classes.yaml     inference-high (1000) / training-low (100)
├── controller/                 Deployment management CLI
│   ├── main.py
│   ├── deployer.py
│   ├── quota_manager.py
│   └── requirements.txt
├── profiler/                   Live resource monitoring dashboard
│   ├── main.py
│   ├── collector.py
│   ├── display.py
│   ├── requirements.txt
│   └── sessions/               Auto-created; stores JSONL session dumps
└── scripts/
    ├── setup.sh                Full cluster bootstrap
    └── teardown.sh             Cluster teardown
```

---

## Prerequisites

- [Docker Desktop](https://www.docker.com/products/docker-desktop/)
- [minikube](https://minikube.sigs.k8s.io/docs/start/)
- [kubectl](https://kubernetes.io/docs/tasks/tools/)
- Python 3.11+

---

## Setup

```bash
bash scripts/setup.sh
```

This script:
1. Starts minikube (`--cpus 4 --memory 6144 --driver=docker`)
2. Enables the metrics-server addon
3. Builds and loads the three workload images into minikube
4. Applies all K8s manifests (namespaces, quotas, priority classes)
5. Creates Python venvs and installs dependencies for `controller/` and `profiler/`

> **Note:** The metrics-server takes ~60 seconds to start collecting after setup. The profiler handles this gracefully and shows *"waiting for metrics server..."* until data is available.

---

## Controller CLI

Manage workload deployments from the `controller/` directory (activate `.venv` first):

```bash
cd controller
source .venv/bin/activate   # Windows: .venv\Scripts\activate

# Deploy a workload
python main.py deploy ../recipes/inference-standard.yaml

# List all running workloads
python main.py list --namespace all

# Check quota usage across both tenants
python main.py quota --namespace all

# Stress test: deploy a recipe with many replicas
python main.py overload ../recipes/training-noisy.yaml --replicas 3

# Delete a specific deployment
python main.py delete training-noisy --namespace beta

# Remove all deployments from a namespace
python main.py purge --namespace beta
```

`--namespace` accepts short forms: `alpha`, `beta`, or `all`.

---

## Profiler Dashboard

Live Rich terminal dashboard showing per-pod CPU/memory utilization and OOMKill events:

```bash
cd profiler
source .venv/bin/activate   # Windows: .venv\Scripts\activate

python main.py --namespace all --interval 5

# Dump session to an auto-named file in profiler/sessions/
python main.py --namespace all --dump

# Or specify a path explicitly
python main.py --namespace all --dump ../reports/my-session.jsonl
```

The dashboard has two panels:
- **Top:** Pod table with CPU%, Mem% colored green/yellow/red by utilization threshold
- **Bottom:** Scrolling OOMKill event log with timestamps

Session dumps are JSONL files — one JSON object per poll interval, plus `session_start` and `session_end` metadata lines. Files are named `session_YYYYMMDD_HHMMSS.jsonl` and written to `profiler/sessions/` automatically.

---

## Demonstrating the Three Success Metrics

### 1. Resource Isolation
Deploy noisy training workloads into `tenant-beta` and verify `tenant-alpha` is unaffected:
```bash
python controller/main.py overload recipes/training-noisy.yaml --replicas 3
python controller/main.py quota --namespace all
# beta namespace approaches 100% CPU/memory; alpha quota unchanged
```

### 2. OOMKill Detection
Deploy the noisy recipe (700MB allocation in a 512Mi-limited container):
```bash
python controller/main.py deploy recipes/training-noisy.yaml
python profiler/main.py --namespace all --dump
# Profiler shows pod status OOMKilled in bold red; OOM log panel updates
# Session saved to profiler/sessions/session_YYYYMMDD_HHMMSS.jsonl
```

### 3. Priority Scheduling
Fill `tenant-beta` with low-priority workloads, then deploy a high-priority inference pod:
```bash
python controller/main.py overload recipes/training-heavy.yaml --replicas 4
python controller/main.py deploy recipes/inference-standard.yaml
python controller/main.py list --namespace all
# inference-standard (priority 1000) schedules immediately;
# training pods remain pending if cluster is under pressure
```

---

## Teardown

```bash
bash scripts/teardown.sh                  # Delete namespaces and priority classes
bash scripts/teardown.sh --stop-minikube  # Also stop the minikube cluster
bash scripts/teardown.sh --delete-minikube # Delete the cluster entirely
```
