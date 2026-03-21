"""
profiler/main.py – CLI entry point for KubeAI-Sentry profiler.

Usage:
  python main.py [--namespace all|tenant-alpha|tenant-beta] [--interval 5]
  python main.py --dump                        # auto-named file in sessions/
  python main.py --dump path/to/file.jsonl     # explicit path
"""
import argparse
import sys
from datetime import datetime
from pathlib import Path

NAMESPACE_MAP = {
    "alpha": "tenant-alpha",
    "beta": "tenant-beta",
    "tenant-alpha": "tenant-alpha",
    "tenant-beta": "tenant-beta",
    "all": "all",
}

ALL_NAMESPACES = ["tenant-alpha", "tenant-beta"]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="kubeai-sentry-profiler",
        description="KubeAI-Sentry Profiler: Live resource monitoring and OOMKill detection",
    )
    parser.add_argument(
        "--namespace", "-n",
        default="all",
        metavar="NS",
        help="Namespace(s) to monitor: all|alpha|beta|tenant-alpha|tenant-beta (default: all)",
    )
    parser.add_argument(
        "--interval", "-i",
        type=int,
        default=5,
        metavar="SECONDS",
        help="Polling interval in seconds (default: 5)",
    )
    parser.add_argument(
        "--dump",
        metavar="FILE",
        nargs="?",
        const="auto",
        default=None,
        help="Write session snapshots in JSONL format. Omit FILE to auto-name in sessions/",
    )
    return parser


def resolve_namespaces(ns_arg: str) -> list[str]:
    """Resolve namespace argument to list of namespace strings."""
    ns = NAMESPACE_MAP.get(ns_arg, ns_arg)
    if ns == "all":
        return ALL_NAMESPACES
    return [ns]


def main():
    parser = build_parser()
    args = parser.parse_args()

    namespaces = resolve_namespaces(args.namespace)
    interval = max(1, args.interval)  # Minimum 1 second

    dump_path = None
    if args.dump is not None:
        if args.dump == "auto":
            sessions_dir = Path(__file__).parent / "sessions"
            sessions_dir.mkdir(exist_ok=True)
            ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
            dump_path = str(sessions_dir / f"session_{ts}.jsonl")
        else:
            dump_path = args.dump
            Path(dump_path).parent.mkdir(parents=True, exist_ok=True)

    from display import Dashboard
    dashboard = Dashboard(namespaces=namespaces, interval=interval, dump_path=dump_path)

    try:
        dashboard.run()
    except KeyboardInterrupt:
        pass
    except Exception as e:
        from rich.console import Console
        Console().print(f"[bold red]Fatal error:[/bold red] {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
