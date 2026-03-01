#!/usr/bin/env bash
# teardown.sh – Cluster teardown for KubeAI-Sentry
# Run from the project root: bash scripts/teardown.sh
# Options:
#   --stop-minikube   Also stop the minikube cluster (default: keep running)
#   --delete-minikube Delete minikube cluster entirely (implies --stop-minikube)
set -euo pipefail

STOP_MINIKUBE=false
DELETE_MINIKUBE=false

for arg in "$@"; do
  case "$arg" in
    --stop-minikube)   STOP_MINIKUBE=true ;;
    --delete-minikube) DELETE_MINIKUBE=true; STOP_MINIKUBE=true ;;
    *)
      echo "Unknown argument: $arg"
      echo "Usage: bash teardown.sh [--stop-minikube] [--delete-minikube]"
      exit 1
      ;;
  esac
done

echo "=== KubeAI-Sentry Cluster Teardown ==="

# ──────────────────────────────────────────────────
# 1. Delete tenant namespaces (cascades to all resources)
# ──────────────────────────────────────────────────
echo ""
echo "[1/3] Deleting tenant namespaces (this cascades to all pods, deployments, quotas)..."

for ns in tenant-alpha tenant-beta; do
  if kubectl get namespace "$ns" &>/dev/null; then
    echo "  Deleting namespace: $ns"
    kubectl delete namespace "$ns" --grace-period=30 &
  else
    echo "  Namespace not found (already deleted): $ns"
  fi
done

# Wait for namespace deletion to complete
echo "  Waiting for namespace deletion..."
for ns in tenant-alpha tenant-beta; do
  kubectl wait --for=delete namespace/"$ns" --timeout=120s 2>/dev/null || true
done
echo "  Namespaces deleted."

# ──────────────────────────────────────────────────
# 2. Remove priority classes
# ──────────────────────────────────────────────────
echo ""
echo "[2/3] Removing priority classes..."
kubectl delete priorityclass inference-high training-low --ignore-not-found=true
echo "  Priority classes removed."

# ──────────────────────────────────────────────────
# 3. Optionally stop/delete minikube
# ──────────────────────────────────────────────────
echo ""
if $DELETE_MINIKUBE; then
  echo "[3/3] Deleting minikube cluster..."
  minikube delete
  echo "  minikube cluster deleted."
elif $STOP_MINIKUBE; then
  echo "[3/3] Stopping minikube cluster..."
  minikube stop
  echo "  minikube cluster stopped."
else
  echo "[3/3] Skipping minikube stop (pass --stop-minikube to stop, --delete-minikube to delete)."
fi

echo ""
echo "=== Teardown Complete ==="
