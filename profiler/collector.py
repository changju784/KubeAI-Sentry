"""
collector.py – Poll Metrics API and watch events for OOMKill detection.
"""
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from kubernetes import client, config
from kubernetes.client.rest import ApiException

NAMESPACES = ["tenant-alpha", "tenant-beta"]


def _load_k8s_config():
    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config()


@dataclass
class PodMetric:
    pod: str
    namespace: str
    cpu_used_cores: float      # actual CPU usage in cores
    cpu_limit_cores: float     # CPU limit from pod spec
    mem_used_bytes: float      # actual memory usage in bytes
    mem_limit_bytes: float     # memory limit from pod spec
    status: str                # "Running", "OOMKilled", "Pending", "Unknown", etc.
    oom_killed: bool = False

    @property
    def cpu_percent(self) -> float:
        if self.cpu_limit_cores <= 0:
            return 0.0
        return (self.cpu_used_cores / self.cpu_limit_cores) * 100.0

    @property
    def mem_percent(self) -> float:
        if self.mem_limit_bytes <= 0:
            return 0.0
        return (self.mem_used_bytes / self.mem_limit_bytes) * 100.0


@dataclass
class OOMEvent:
    timestamp: datetime
    pod: str
    namespace: str
    container: str
    exit_code: int
    message: str = ""


def _parse_quantity(value: str) -> float:
    """Convert Kubernetes quantity string to float (CPU in cores, memory in bytes)."""
    if not value:
        return 0.0

    mem_suffixes = {
        "Ki": 1024,
        "Mi": 1024 ** 2,
        "Gi": 1024 ** 3,
        "Ti": 1024 ** 4,
        "K": 1000,
        "M": 1000 ** 2,
        "G": 1000 ** 3,
    }
    for suffix, multiplier in mem_suffixes.items():
        if value.endswith(suffix):
            return float(value[: -len(suffix)]) * multiplier

    if value.endswith("m"):
        return float(value[:-1]) / 1000.0

    if value.endswith("n"):  # nanocores from metrics API
        return float(value[:-1]) / 1_000_000_000.0

    try:
        return float(value)
    except ValueError:
        return 0.0


def get_pod_specs(namespace: str) -> dict[str, dict]:
    """Fetch resource limits and pod status from pod specs.

    Returns dict: pod_name -> {cpu_limit, mem_limit, status, oom_killed, containers}
    """
    _load_k8s_config()
    core_v1 = client.CoreV1Api()

    try:
        pods = core_v1.list_namespaced_pod(namespace=namespace)
    except ApiException:
        return {}

    specs = {}
    for pod in pods.items:
        pod_name = pod.metadata.name
        containers = pod.spec.containers or []

        # Sum limits across containers
        total_cpu_limit = 0.0
        total_mem_limit = 0.0
        container_names = []

        for c in containers:
            container_names.append(c.name)
            if c.resources and c.resources.limits:
                total_cpu_limit += _parse_quantity(c.resources.limits.get("cpu", "0"))
                total_mem_limit += _parse_quantity(c.resources.limits.get("memory", "0"))

        # Determine pod status and OOM state
        phase = pod.status.phase or "Unknown"
        oom_killed = False
        pod_status = phase

        if pod.status.container_statuses:
            for cs in pod.status.container_statuses:
                # Check current state
                if cs.state and cs.state.terminated:
                    if cs.state.terminated.reason == "OOMKilled":
                        oom_killed = True
                        pod_status = "OOMKilled"
                # Check last state (pod restarted after OOMKill)
                if cs.last_state and cs.last_state.terminated:
                    if cs.last_state.terminated.reason == "OOMKilled":
                        oom_killed = True
                        if pod_status not in ("OOMKilled",):
                            pod_status = "Restarted(OOM)"

        specs[pod_name] = {
            "cpu_limit": total_cpu_limit,
            "mem_limit": total_mem_limit,
            "status": pod_status,
            "oom_killed": oom_killed,
            "containers": container_names,
            "namespace": namespace,
        }

    return specs


def get_pod_metrics(namespace: str) -> list[PodMetric]:
    """Call metrics.k8s.io API to get current CPU/memory usage per pod.

    Returns list of PodMetric objects. Handles missing metrics server gracefully.
    """
    _load_k8s_config()
    custom_api = client.CustomObjectsApi()

    # Fetch specs for limits and status
    specs = get_pod_specs(namespace)

    try:
        metrics_response = custom_api.list_namespaced_custom_object(
            group="metrics.k8s.io",
            version="v1beta1",
            namespace=namespace,
            plural="pods",
        )
    except ApiException as e:
        if e.status in (404, 503):
            # Metrics server not ready – return specs only with zero usage
            results = []
            for pod_name, spec in specs.items():
                results.append(PodMetric(
                    pod=pod_name,
                    namespace=namespace,
                    cpu_used_cores=0.0,
                    cpu_limit_cores=spec["cpu_limit"],
                    mem_used_bytes=0.0,
                    mem_limit_bytes=spec["mem_limit"],
                    status=spec["status"],
                    oom_killed=spec["oom_killed"],
                ))
            return results
        raise

    pod_items = metrics_response.get("items", [])
    results = []

    for item in pod_items:
        pod_name = item["metadata"]["name"]
        spec = specs.get(pod_name, {})

        # Sum CPU and memory across containers
        total_cpu = 0.0
        total_mem = 0.0
        for container in item.get("containers", []):
            usage = container.get("usage", {})
            total_cpu += _parse_quantity(usage.get("cpu", "0"))
            total_mem += _parse_quantity(usage.get("memory", "0"))

        results.append(PodMetric(
            pod=pod_name,
            namespace=namespace,
            cpu_used_cores=total_cpu,
            cpu_limit_cores=spec.get("cpu_limit", 0.0),
            mem_used_bytes=total_mem,
            mem_limit_bytes=spec.get("mem_limit", 0.0),
            status=spec.get("status", "Unknown"),
            oom_killed=spec.get("oom_killed", False),
        ))

    # Include pods not in metrics (e.g., Pending/OOMKilled with no metrics)
    metrics_pod_names = {item["metadata"]["name"] for item in pod_items}
    for pod_name, spec in specs.items():
        if pod_name not in metrics_pod_names:
            results.append(PodMetric(
                pod=pod_name,
                namespace=namespace,
                cpu_used_cores=0.0,
                cpu_limit_cores=spec["cpu_limit"],
                mem_used_bytes=0.0,
                mem_limit_bytes=spec["mem_limit"],
                status=spec["status"],
                oom_killed=spec["oom_killed"],
            ))

    return results


def detect_oomkill(namespace: str) -> list[OOMEvent]:
    """Scan containerStatuses for OOMKill events.

    Returns list of OOMEvent objects for any container that was OOMKilled.
    """
    _load_k8s_config()
    core_v1 = client.CoreV1Api()

    try:
        pods = core_v1.list_namespaced_pod(namespace=namespace)
    except ApiException:
        return []

    events = []
    for pod in pods.items:
        if not pod.status.container_statuses:
            continue

        for cs in pod.status.container_statuses:
            # Check current terminated state
            terminated = None
            if cs.state and cs.state.terminated:
                terminated = cs.state.terminated
            elif cs.last_state and cs.last_state.terminated:
                terminated = cs.last_state.terminated

            if terminated and terminated.reason == "OOMKilled":
                ts = terminated.finished_at or datetime.utcnow()
                events.append(OOMEvent(
                    timestamp=ts,
                    pod=pod.metadata.name,
                    namespace=namespace,
                    container=cs.name,
                    exit_code=terminated.exit_code or 137,
                    message=f"OOMKilled at {ts} (exit {terminated.exit_code})",
                ))

    return events


def collect_all_namespaces(namespaces: list[str]) -> tuple[list[PodMetric], list[OOMEvent]]:
    """Collect metrics and OOM events across multiple namespaces."""
    all_metrics = []
    all_oom = []

    for ns in namespaces:
        try:
            metrics = get_pod_metrics(ns)
            all_metrics.extend(metrics)
        except Exception:
            pass

        try:
            oom_events = detect_oomkill(ns)
            all_oom.extend(oom_events)
        except Exception:
            pass

    return all_metrics, all_oom
