"""
quota_manager.py – Read and display ResourceQuota usage via kubernetes-client.
"""
from typing import Optional

from kubernetes import client, config
from kubernetes.client.rest import ApiException
from rich.console import Console
from rich.table import Table
from rich import box

console = Console()

NAMESPACES = ["tenant-alpha", "tenant-beta"]


def _load_k8s_config():
    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config()


def _parse_quantity(value: str) -> float:
    """Convert Kubernetes quantity string to float (CPU in cores, memory in bytes)."""
    if not value:
        return 0.0

    # Memory suffixes
    mem_suffixes = {
        "Ki": 1024,
        "Mi": 1024 ** 2,
        "Gi": 1024 ** 3,
        "Ti": 1024 ** 4,
        "K": 1000,
        "M": 1000 ** 2,
        "G": 1000 ** 3,
        "T": 1000 ** 4,
    }
    for suffix, multiplier in mem_suffixes.items():
        if value.endswith(suffix):
            return float(value[: -len(suffix)]) * multiplier

    # CPU millicores
    if value.endswith("m"):
        return float(value[:-1]) / 1000.0

    return float(value)


def _format_memory(bytes_val: float) -> str:
    """Format bytes as human-readable string."""
    if bytes_val >= 1024 ** 3:
        return f"{bytes_val / 1024 ** 3:.2f}Gi"
    if bytes_val >= 1024 ** 2:
        return f"{bytes_val / 1024 ** 2:.2f}Mi"
    return f"{bytes_val:.0f}B"


def _format_cpu(cores: float) -> str:
    if cores < 1.0:
        return f"{cores * 1000:.0f}m"
    return f"{cores:.2f}"


def get_quota_status(namespace: str) -> list[dict]:
    """Read ResourceQuota hard/used values for a namespace.

    Returns list of quota dicts with keys:
      name, namespace, resource, used, hard, used_raw, hard_raw, percent
    """
    _load_k8s_config()
    core_v1 = client.CoreV1Api()

    try:
        quotas = core_v1.list_namespaced_resource_quota(namespace=namespace)
    except ApiException as e:
        if e.status == 404:
            return []
        raise

    results = []
    for quota in quotas.items:
        hard = quota.status.hard or {}
        used = quota.status.used or {}

        for resource, hard_val in hard.items():
            used_val = used.get(resource, "0")
            hard_raw = _parse_quantity(hard_val)
            used_raw = _parse_quantity(used_val)
            percent = (used_raw / hard_raw * 100) if hard_raw > 0 else 0.0

            # Format display values
            if "memory" in resource:
                used_display = _format_memory(used_raw)
                hard_display = _format_memory(hard_raw)
            elif "cpu" in resource:
                used_display = _format_cpu(used_raw)
                hard_display = _format_cpu(hard_raw)
            else:
                used_display = used_val
                hard_display = hard_val

            results.append({
                "quota_name": quota.metadata.name,
                "namespace": namespace,
                "resource": resource,
                "used": used_display,
                "hard": hard_display,
                "used_raw": used_raw,
                "hard_raw": hard_raw,
                "percent": percent,
            })

    return results


def _percent_color(pct: float) -> str:
    if pct >= 90:
        return "bold red"
    if pct >= 70:
        return "yellow"
    return "green"


def print_quota_table(namespace: Optional[str] = None):
    """Print a Rich table showing quota usage for one or all namespaces."""
    namespaces = NAMESPACES if namespace in (None, "all") else [namespace]

    table = Table(
        title="ResourceQuota Status",
        box=box.ROUNDED,
        show_header=True,
        header_style="bold cyan",
    )
    table.add_column("Namespace", style="cyan", min_width=15)
    table.add_column("Quota", style="dim", min_width=15)
    table.add_column("Resource", min_width=20)
    table.add_column("Used", justify="right", min_width=10)
    table.add_column("Limit", justify="right", min_width=10)
    table.add_column("Used %", justify="right", min_width=8)

    any_data = False
    for ns in namespaces:
        rows = get_quota_status(ns)
        if not rows:
            table.add_row(ns, "—", "No quota found", "—", "—", "—")
            continue

        any_data = True
        for row in rows:
            pct = row["percent"]
            pct_style = _percent_color(pct)
            table.add_row(
                row["namespace"],
                row["quota_name"],
                row["resource"],
                row["used"],
                row["hard"],
                f"[{pct_style}]{pct:.1f}%[/{pct_style}]",
            )

    console.print(table)
