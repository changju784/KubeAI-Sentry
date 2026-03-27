# KubeAI-Sentry: Multi-Tenant AI Workload Lifecycle Simulation
**Date:** 2026-03-01
**Platform:** Windows 10 Home 22H2, Docker Desktop, minikube v1.38.1, Kubernetes v1.35.1

---

## 1. Overview

KubeAI-Sentry is a local simulation of AI workload lifecycle management in a multi-tenant
Kubernetes cluster. The project addresses a practical constraint: real AI infrastructure
requires GPU hardware and VRAM that is expensive and inaccessible for development and research.

The approach taken here is to use **CPU and RAM as proxies for GPU and VRAM**. Each AI workload
type is modeled as a Python process with realistic resource consumption patterns, containerized
and deployed into a Kubernetes cluster running locally via minikube. This allows full
end-to-end testing of cluster-level behaviors — resource isolation, scheduling priority,
and memory limit enforcement — without any specialized hardware.

---

## 2. Infrastructure

### 2.1 Cluster

| Component | Value |
|---|---|
| Kubernetes version | v1.35.1 |
| Runtime | minikube v1.38.1, Docker driver |
| Node resources | 2 vCPU, 2048 MB RAM |
| Metrics collection | metrics-server addon (15s scrape interval) |

### 2.2 Tenants and Quotas

Two isolated namespaces simulate separate organizational tenants:

| Namespace | CPU Quota | Memory Quota | Max Pods |
|---|---|---|---|
| `tenant-alpha` | 1 core (requests + limits) | 512 Mi | 3 |
| `tenant-beta` | 2 cores (requests + limits) | 1 Gi | 5 |

### 2.3 Scheduling Priority Classes

| Priority Class | Value | Preemption Policy |
|---|---|---|
| `inference-high` | 1000 | PreemptLowerPriority |
| `training-low` | 100 | Never |

Inference workloads in `tenant-alpha` use `inference-high`, giving them a 10x scheduling
priority advantage over training and data-cleansing workloads.

---

## 3. Workload Design

Each workload is a containerized Python process (Python 3.11-slim base image) configured
via environment variables. All three share a common pattern: allocate a fixed memory block
at startup by writing to every page (forcing physical allocation), then run a CPU loop
at a configured duty cycle.

### 3.1 Mock Inference (`mock-inference`)

Simulates steady-state AI inference serving — a model loaded into memory, continuously
processing requests at a stable throughput.

| Parameter | Value |
|---|---|
| `LOAD_PROFILE` | `steady` |
| `MEMORY_TARGET_MB` | 64 MB |
| `CPU_CORES` | 0.15 |
| CPU limit | 200m |
| Memory limit | 128 Mi |
| Namespace | `tenant-alpha` |
| Priority class | `inference-high` (1000) |

**CPU mechanism:** Spawns worker threads that alternate between a math busy-loop
(`math.sqrt` of a sum-of-squares) and a sleep period, targeting a 20–30% duty cycle
per thread.

### 3.2 Mock Training (`mock-training`)

Simulates a GPU training job holding a model in memory with bursty compute
(forward pass + backprop) alternating with low-CPU checkpoint saves.

| Parameter | Value |
|---|---|
| `LOAD_PROFILE` | `burst` |
| `MEMORY_TARGET_MB` | 192 MB |
| `CPU_CORES` | 0.3 |
| CPU limit | 400m |
| Memory limit | 256 Mi |
| Namespace | `tenant-beta` |
| Priority class | `training-low` (100) |

**CPU mechanism:** Alternates between a high-CPU phase (labeled TRAINING)
and a low-CPU phase (labeled CHECKPOINT) every 30 seconds.

### 3.3 Mock Data Cleansing (`mock-data-cleansing`)

Simulates an I/O-bound data pipeline: light CPU for transformation logic, most time
spent waiting on simulated disk or network I/O.

| Parameter | Value |
|---|---|
| `LOAD_PROFILE` | `steady` |
| `MEMORY_TARGET_MB` | 32 MB |
| `CPU_CORES` | 0.1 |
| CPU limit | 150m |
| Memory limit | 96 Mi |
| Namespace | `tenant-beta` |
| Priority class | `training-low` (100) |

**CPU mechanism:** Uses a 200ms cycle with a high sleep-to-work ratio, simulating
I/O wait. Periodically logs batch completions every 10 seconds.

### 3.4 Mock Training Noisy (`mock-training` / noisy recipe)

A deliberately misconfigured training deployment used to trigger OOMKill. Uses the
same `mock-training` image but with a memory target that exceeds the container limit.

| Parameter | Value |
|---|---|
| `MEMORY_TARGET_MB` | 300 MB |
| Memory limit | 256 Mi (~268 MB) |
| Delta | +32 MB over limit |

The process attempts to allocate 300 MB and touch every page, forcing the Linux kernel's
OOM killer to terminate the container before allocation completes.

---

## 4. Methodology

### 4.1 Image Loading

Docker images were built locally using `docker build` and loaded into minikube's internal
image registry using `minikube image load`. No external container registry was used.
`imagePullPolicy: Never` is set on all deployments to enforce this.

### 4.2 Deployment Management

A custom Python controller (`controller/main.py`) wraps the Kubernetes Python client
to apply WorkloadRecipe YAML files as Kubernetes Deployments. Recipes define workload
type, tenant namespace, replica count, resource limits, and environment configuration
in a single declarative file.

### 4.3 Observability

A live terminal dashboard (`profiler/main.py`) polls `metrics.k8s.io` every few seconds
and displays per-pod CPU and RAM usage using the Rich library. OOMKill events are detected
by inspecting `containerStatuses.lastState.terminated.reason` and displayed in red.

---

## 5. Observed Results

All metrics below were observed live via `kubectl top pods --all-namespaces` and the
profiler dashboard during the session on 2026-03-01.

### 5.1 Steady-State Metrics

| Workload | Namespace | CPU Observed | CPU % of Limit | RAM Observed | RAM % of Limit | Status |
|---|---|---|---|---|---|---|
| `inference-standard` | tenant-alpha | 25–35m | ~15% | 68 Mi | 53% | Running |
| `data-cleansing` | tenant-beta | 10–15m | ~8% | 34 Mi | 35% | Running |
| `training-heavy` | tenant-beta | 0–1m | ~0% | 196 Mi | 76% | Running |

**Notes:**
- `inference-standard` consistently consumed ~30m CPU, matching its 0.15-core steady
  load target against a 200m limit.
- `training-heavy` showed near-zero CPU at the moment of observation, consistent with
  being in the CHECKPOINT phase of its 30-second burst cycle. Its ~196 Mi RAM reflects
  its 192 MB allocation target successfully committed.
- `data-cleansing` showed stable low CPU, slightly above its 0.1-core target, reflecting
  thread overhead.

### 5.2 OOMKill Event

The `training-noisy` deployment was applied with `MEMORY_TARGET_MB=300` and a 256 Mi
memory limit. The container attempted to allocate 300 MB and touch every page, causing
the Linux kernel OOM killer to terminate the process before allocation completed.

| Workload | RAM Target | RAM Limit | Outcome |
|---|---|---|---|
| `training-noisy` | 300 MB | 256 Mi (~268 MB) | OOMKilled |

The profiler dashboard displayed the pod status as **OOMKilled** with 0m CPU and 0B RAM
(post-termination), confirming the event was detected and surfaced correctly.

---

## 6. Key Findings

1. **CPU proxy fidelity:** The busy-loop duty-cycle mechanism produced consistent and
   repeatable CPU consumption. Inference held ~15% of its limit; data-cleansing held ~8%.
   Both matched their configured targets closely.

2. **Memory proxy fidelity:** The page-touching allocation strategy forced genuine physical
   memory commitment. `training-heavy` held ~196 Mi of real RAM throughout the session,
   accurately simulating a model loaded into VRAM.

3. **OOMKill trigger reliability:** The noisy training workload reliably triggered OOMKill
   by exceeding its container memory limit (300 MB target vs. 256 Mi limit). The Kubernetes
   control plane detected and reported the termination reason correctly.

4. **Tenant isolation:** The namespace + ResourceQuota architecture successfully isolated
   tenant-alpha from tenant-beta. Workloads in tenant-beta consuming quota did not affect
   the inference workload in tenant-alpha.

5. **Metrics pipeline:** The metrics-server addon required approximately 60 seconds after
   pod startup before data was available via `metrics.k8s.io`. Once available, the profiler
   dashboard reflected live values with low latency.

---

## 7. Limitations

- **Single node:** All workloads run on one minikube node. Real multi-tenant clusters
  span multiple nodes; node-level scheduling and bin-packing effects are not observed here.
- **No actual preemption observed:** The priority class configuration was applied and
  verified, but no scenario was run where `inference-high` actively preempted a running
  `training-low` pod during this session.
- **CPU proxy is approximate:** Python's GIL and OS scheduling introduce variance.
  The duty-cycle approach produces realistic averages but not precise per-core utilization.
- **No persistence:** minikube state (loaded images, running pods) is lost on cluster
  restart. Images must be reloaded after each restart.
