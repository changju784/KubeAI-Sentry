"""
controller/main.py – CLI entry point for KubeAI-Sentry controller.

Usage:
  python main.py deploy <recipe.yaml>
  python main.py list [--namespace alpha|beta|all]
  python main.py delete <name> --namespace <ns>
  python main.py quota [--namespace alpha|beta|all]
  python main.py overload <recipe.yaml> --replicas N
  python main.py purge --namespace <ns>
"""
import argparse
import sys

from rich.console import Console
from rich.table import Table
from rich import box

console = Console()


def cmd_deploy(args):
    from deployer import deploy
    try:
        result = deploy(args.recipe)
        action = result["action"].upper()
        console.print(
            f"[bold green]{action}[/bold green] deployment "
            f"[cyan]{result['name']}[/cyan] in "
            f"[cyan]{result['namespace']}[/cyan] "
            f"({result['replicas']} replica(s))"
        )
    except FileNotFoundError as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        sys.exit(1)
    except Exception as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        sys.exit(1)


def cmd_list(args):
    from deployer import list_workloads
    namespace = getattr(args, "namespace", "all") or "all"

    # Normalize namespace arg
    if namespace not in ("all",) and not namespace.startswith("tenant-"):
        namespace = f"tenant-{namespace}"

    try:
        workloads = list_workloads(namespace)
    except Exception as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        sys.exit(1)

    if not workloads:
        console.print("[yellow]No workloads found.[/yellow]")
        return

    table = Table(
        title=f"Workloads ({namespace})",
        box=box.ROUNDED,
        header_style="bold cyan",
    )
    table.add_column("Name", style="cyan", min_width=20)
    table.add_column("Namespace", min_width=15)
    table.add_column("Type", min_width=15)
    table.add_column("Priority Class", min_width=18)
    table.add_column("Replicas", justify="right")
    table.add_column("Ready", justify="right")
    table.add_column("Status", min_width=14)

    for w in workloads:
        status = w["status"]
        if status == "Running":
            status_str = "[green]Running[/green]"
        elif status == "Pending":
            status_str = "[yellow]Pending[/yellow]"
        else:
            status_str = f"[red]{status}[/red]"

        table.add_row(
            w["name"],
            w["namespace"],
            w["workload_type"],
            w["priority_class"],
            str(w["replicas"]),
            str(w["ready"]),
            status_str,
        )

    console.print(table)


def cmd_delete(args):
    from deployer import delete
    namespace = args.namespace
    if not namespace.startswith("tenant-"):
        namespace = f"tenant-{namespace}"

    try:
        result = delete(args.name, namespace)
        if result["action"] == "deleted":
            console.print(
                f"[bold green]DELETED[/bold green] deployment "
                f"[cyan]{result['name']}[/cyan] from [cyan]{result['namespace']}[/cyan]"
            )
        else:
            console.print(
                f"[yellow]NOT FOUND:[/yellow] deployment [cyan]{result['name']}[/cyan] "
                f"in [cyan]{result['namespace']}[/cyan]"
            )
    except Exception as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        sys.exit(1)


def cmd_quota(args):
    from quota_manager import print_quota_table
    namespace = getattr(args, "namespace", "all") or "all"
    if namespace not in ("all",) and not namespace.startswith("tenant-"):
        namespace = f"tenant-{namespace}"

    try:
        print_quota_table(namespace)
    except Exception as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        sys.exit(1)


def cmd_overload(args):
    from deployer import deploy
    try:
        result = deploy(args.recipe, replicas_override=args.replicas)
        action = result["action"].upper()
        console.print(
            f"[bold yellow]OVERLOAD[/bold yellow] – {action} deployment "
            f"[cyan]{result['name']}[/cyan] in "
            f"[cyan]{result['namespace']}[/cyan] "
            f"with [bold]{result['replicas']}[/bold] replicas"
        )
    except FileNotFoundError as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        sys.exit(1)
    except Exception as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        sys.exit(1)


def cmd_purge(args):
    namespace = args.namespace
    if not namespace.startswith("tenant-"):
        namespace = f"tenant-{namespace}"

    console.print(f"[bold yellow]Purging all deployments in [cyan]{namespace}[/cyan]...[/bold yellow]")

    from deployer import purge
    try:
        results = purge(namespace)
        if not results:
            console.print("[yellow]No deployments found to purge.[/yellow]")
            return
        for r in results:
            console.print(f"  [red]DELETED[/red] {r['name']}")
        console.print(f"[bold green]Purge complete: {len(results)} deployment(s) removed.[/bold green]")
    except Exception as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        sys.exit(1)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="kubeai-sentry-controller",
        description="KubeAI-Sentry: Deploy and manage AI workloads in a multi-tenant Kubernetes cluster",
    )
    subparsers = parser.add_subparsers(dest="command", metavar="<command>")
    subparsers.required = True

    # deploy
    p_deploy = subparsers.add_parser("deploy", help="Deploy a workload from a recipe YAML")
    p_deploy.add_argument("recipe", metavar="<recipe.yaml>", help="Path to WorkloadRecipe YAML")
    p_deploy.set_defaults(func=cmd_deploy)

    # list
    p_list = subparsers.add_parser("list", help="List deployed workloads")
    p_list.add_argument(
        "--namespace", "-n",
        default="all",
        metavar="NS",
        help="Namespace to list (alpha|beta|all, default: all)",
    )
    p_list.set_defaults(func=cmd_list)

    # delete
    p_delete = subparsers.add_parser("delete", help="Delete a deployment")
    p_delete.add_argument("name", metavar="<name>", help="Deployment name")
    p_delete.add_argument("--namespace", "-n", required=True, metavar="NS", help="Namespace")
    p_delete.set_defaults(func=cmd_delete)

    # quota
    p_quota = subparsers.add_parser("quota", help="Show ResourceQuota usage")
    p_quota.add_argument(
        "--namespace", "-n",
        default="all",
        metavar="NS",
        help="Namespace to query (alpha|beta|all, default: all)",
    )
    p_quota.set_defaults(func=cmd_quota)

    # overload
    p_overload = subparsers.add_parser("overload", help="Deploy a recipe with many replicas to stress test")
    p_overload.add_argument("recipe", metavar="<recipe.yaml>", help="Path to WorkloadRecipe YAML")
    p_overload.add_argument("--replicas", "-r", type=int, required=True, metavar="N", help="Number of replicas")
    p_overload.set_defaults(func=cmd_overload)

    # purge
    p_purge = subparsers.add_parser("purge", help="Delete all deployments in a namespace")
    p_purge.add_argument("--namespace", "-n", required=True, metavar="NS", help="Namespace to purge")
    p_purge.set_defaults(func=cmd_purge)

    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
