#!/usr/bin/env bash
# setup.sh – Full cluster bootstrap for KubeAI-Sentry
# Run from the project root: bash scripts/setup.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

echo "=== KubeAI-Sentry Cluster Setup ==="
echo "Project root: $PROJECT_ROOT"

# ──────────────────────────────────────────────────
# 1. Start minikube
# ──────────────────────────────────────────────────
echo ""
echo "[1/5] Starting minikube (cpus=4, memory=6144MB, driver=docker)..."
minikube start --cpus 4 --memory 6144 --driver=docker

echo ""
echo "[2/5] Enabling metrics-server addon..."
minikube addons enable metrics-server
echo "      NOTE: metrics-server may take ~60s to begin collecting data."

# ──────────────────────────────────────────────────
# 2. Build and load Docker images into minikube
# ──────────────────────────────────────────────────
echo ""
echo "[3/5] Building and loading workload images into minikube..."

IMAGES=(
  "mock-inference"
  "mock-training"
  "mock-data-cleansing"
)

for name in "${IMAGES[@]}"; do
  dir="$PROJECT_ROOT/docker/$name"
  image_tag="kubeai-sentry/$name:latest"

  echo "  Building $image_tag from $dir..."
  docker build -t "$image_tag" "$dir"

  echo "  Loading $image_tag into minikube..."
  minikube image load "$image_tag"

  echo "  Done: $image_tag"
done

# ──────────────────────────────────────────────────
# 3. Apply Kubernetes manifests
# ──────────────────────────────────────────────────
echo ""
echo "[4/5] Applying Kubernetes manifests..."

echo "  Applying priority classes..."
kubectl apply -f "$PROJECT_ROOT/k8s/priority-classes.yaml"

echo "  Applying namespaces..."
kubectl apply -f "$PROJECT_ROOT/k8s/namespaces/"

echo "  Applying resource quotas and limit ranges..."
kubectl apply -f "$PROJECT_ROOT/k8s/quotas/"

# ──────────────────────────────────────────────────
# 4. Install Python dependencies
# ──────────────────────────────────────────────────
echo ""
echo "[5/5] Setting up Python virtual environments..."

setup_venv() {
  local component="$1"
  local dir="$PROJECT_ROOT/$component"

  echo "  Setting up venv for $component..."
  python -m venv "$dir/.venv"

  if [[ "$OSTYPE" == "msys" || "$OSTYPE" == "cygwin" || "$OSTYPE" == "win32" ]]; then
    "$dir/.venv/Scripts/pip" install -r "$dir/requirements.txt" --quiet
  else
    "$dir/.venv/bin/pip" install -r "$dir/requirements.txt" --quiet
  fi
  echo "  Done: $component"
}

setup_venv "controller"
setup_venv "profiler"

# ──────────────────────────────────────────────────
# 5. Verify cluster state
# ──────────────────────────────────────────────────
echo ""
echo "=== Cluster Status ==="
kubectl get namespaces | grep -E "tenant|NAME"
echo ""
kubectl get resourcequota --all-namespaces 2>/dev/null || true
echo ""
kubectl get priorityclass | grep -E "inference|training|NAME" || true

echo ""
echo "=== Setup Complete ==="
echo ""
echo "Next steps:"
echo "  Deploy inference workload:"
echo "    python controller/main.py deploy recipes/inference-standard.yaml"
echo ""
echo "  List all workloads:"
echo "    python controller/main.py list --namespace all"
echo ""
echo "  Trigger OOMKill scenario:"
echo "    python controller/main.py overload recipes/training-noisy.yaml --replicas 3"
echo ""
echo "  Start profiler dashboard:"
echo "    python profiler/main.py --namespace all"
echo ""
echo "  Check quota usage:"
echo "    python controller/main.py quota --namespace all"
echo ""
echo "  Teardown:"
echo "    bash scripts/teardown.sh"
