"""
display.py – Rich Live dashboard for KubeAI-Sentry profiler.

Layout:
  Top panel:    Live Table – Pod | NS | CPU Used | CPU% | Mem Used | Mem% | Status
  Bottom panel: Scrolling OOMKill event log with timestamps
"""
from collections import deque
from datetime import datetime
from typing import Optional

from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich import box
from rich.text import Text

from collector import PodMetric, OOMEvent

console = Console()

MAX_OOM_LOG_LINES = 20  # Keep last N OOM events in log


def _format_cpu(cores: float) -> str:
    if cores < 1.0:
        return f"{cores * 1000:.0f}m"
    return f"{cores:.3f}"


def _format_memory(bytes_val: float) -> str:
    if bytes_val >= 1024 ** 3:
        return f"{bytes_val / 1024 ** 3:.2f}Gi"
    if bytes_val >= 1024 ** 2:
        return f"{bytes_val / 1024 ** 2:.1f}Mi"
    if bytes_val >= 1024:
        return f"{bytes_val / 1024:.0f}Ki"
    return f"{bytes_val:.0f}B"


def _percent_style(pct: float, oom: bool = False) -> str:
    """Return Rich markup style based on utilization percentage."""
    if oom:
        return "bold red"
    if pct >= 85:
        return "bold red"
    if pct >= 60:
        return "yellow"
    return "green"


def _status_style(status: str) -> str:
    s = status.lower()
    if "oomkill" in s:
        return "bold red"
    if "running" in s:
        return "green"
    if "pending" in s:
        return "yellow"
    if "restarted" in s:
        return "bold orange3"
    return "dim"


def build_metrics_table(metrics: list[PodMetric], metrics_ready: bool) -> Table:
    """Build a Rich Table from a list of PodMetric objects."""
    title = "Pod Resource Utilization"
    if not metrics_ready:
        title += "  [yellow](waiting for metrics server...)[/yellow]"

    table = Table(
        title=title,
        box=box.ROUNDED,
        header_style="bold cyan",
        show_lines=True,
        expand=True,
    )
    table.add_column("Pod", style="cyan", min_width=30, no_wrap=True)
    table.add_column("Namespace", min_width=14)
    table.add_column("CPU Used", justify="right", min_width=9)
    table.add_column("CPU%", justify="right", min_width=7)
    table.add_column("Mem Used", justify="right", min_width=9)
    table.add_column("Mem%", justify="right", min_width=7)
    table.add_column("Status", min_width=14)

    if not metrics:
        table.add_row(
            "[dim]No pods found[/dim]", "—", "—", "—", "—", "—", "—"
        )
        return table

    # Sort: OOMKilled first, then by namespace, then by name
    sorted_metrics = sorted(
        metrics,
        key=lambda m: (0 if m.oom_killed else 1, m.namespace, m.pod),
    )

    for m in sorted_metrics:
        cpu_pct = m.cpu_percent
        mem_pct = m.mem_percent

        cpu_style = _percent_style(cpu_pct, m.oom_killed)
        mem_style = _percent_style(mem_pct, m.oom_killed)
        status_style = _status_style(m.status)

        cpu_used_str = _format_cpu(m.cpu_used_cores) if metrics_ready else "—"
        mem_used_str = _format_memory(m.mem_used_bytes) if metrics_ready else "—"
        cpu_pct_str = f"[{cpu_style}]{cpu_pct:.1f}%[/{cpu_style}]" if metrics_ready else "[dim]—[/dim]"
        mem_pct_str = f"[{mem_style}]{mem_pct:.1f}%[/{mem_style}]" if metrics_ready else "[dim]—[/dim]"

        table.add_row(
            m.pod,
            m.namespace,
            cpu_used_str,
            cpu_pct_str,
            mem_used_str,
            mem_pct_str,
            f"[{status_style}]{m.status}[/{status_style}]",
        )

    return table


def build_oom_log_panel(oom_log: deque) -> Panel:
    """Build a Rich Panel with the OOM event log."""
    if not oom_log:
        content = Text("No OOMKill events detected.", style="dim green")
    else:
        lines = []
        for event in oom_log:
            ts = event.timestamp
            if hasattr(ts, "strftime"):
                ts_str = ts.strftime("%H:%M:%S")
            else:
                ts_str = str(ts)[:8]
            line = Text()
            line.append(f"[{ts_str}] ", style="dim")
            line.append("OOMKilled ", style="bold red")
            line.append(f"{event.pod}", style="cyan")
            line.append(f" ({event.container})", style="dim")
            line.append(f" in {event.namespace}", style="yellow")
            lines.append(line)

        content = Text("\n").join(lines)

    return Panel(
        content,
        title="[bold red]OOMKill Event Log[/bold red]",
        border_style="red",
        padding=(0, 1),
    )


class Dashboard:
    """Rich Live dashboard that polls metrics and displays them in real time."""

    def __init__(self, namespaces: list[str], interval: int = 5):
        self.namespaces = namespaces
        self.interval = interval
        self.oom_log: deque[OOMEvent] = deque(maxlen=MAX_OOM_LOG_LINES)
        self._seen_oom_keys: set[str] = set()

    def _make_layout(self, metrics: list[PodMetric], metrics_ready: bool) -> Layout:
        layout = Layout()
        layout.split_column(
            Layout(name="metrics", ratio=3),
            Layout(name="oom_log", ratio=1, minimum_size=5),
        )
        layout["metrics"].update(build_metrics_table(metrics, metrics_ready))
        layout["oom_log"].update(build_oom_log_panel(self.oom_log))
        return layout

    def _update_oom_log(self, oom_events: list[OOMEvent]):
        for event in oom_events:
            key = f"{event.namespace}/{event.pod}/{event.container}/{event.exit_code}"
            if key not in self._seen_oom_keys:
                self._seen_oom_keys.add(key)
                self.oom_log.append(event)

    def run(self):
        """Start the live dashboard loop."""
        from collector import collect_all_namespaces

        console.print(
            f"[bold cyan]KubeAI-Sentry Profiler[/bold cyan] – "
            f"watching {self.namespaces} every {self.interval}s  "
            "[dim](Ctrl+C to exit)[/dim]"
        )

        # Initial collection
        metrics, oom_events = collect_all_namespaces(self.namespaces)
        self._update_oom_log(oom_events)
        metrics_ready = any(m.cpu_used_cores > 0 or m.mem_used_bytes > 0 for m in metrics)

        with Live(
            self._make_layout(metrics, metrics_ready),
            console=console,
            refresh_per_second=1,
            screen=False,
        ) as live:
            import time
            last_refresh = time.monotonic()

            try:
                while True:
                    time.sleep(0.5)  # Check frequently for smooth refresh

                    if time.monotonic() - last_refresh >= self.interval:
                        metrics, oom_events = collect_all_namespaces(self.namespaces)
                        self._update_oom_log(oom_events)
                        metrics_ready = any(
                            m.cpu_used_cores > 0 or m.mem_used_bytes > 0 for m in metrics
                        )
                        live.update(self._make_layout(metrics, metrics_ready))
                        last_refresh = time.monotonic()

            except KeyboardInterrupt:
                pass

        console.print("\n[dim]Profiler stopped.[/dim]")
