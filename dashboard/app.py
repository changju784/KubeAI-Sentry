"""
dashboard/app.py – Streamlit GUI for KubeAI-Sentry.

Run:
  cd dashboard
  pip install -r requirements.txt
  streamlit run app.py

Get the minikube dashboard URL first:
  minikube dashboard --url
"""
import json
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

# ── Path setup ──────────────────────────────────────────────────────────────
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "controller"))
sys.path.insert(0, str(ROOT / "profiler"))

# ── Page config ─────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="KubeAI-Sentry",
    page_icon="🛡️",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── Constants ────────────────────────────────────────────────────────────────
RECIPES = {
    "Inference Standard  (alpha · inference-high)":   str(ROOT / "recipes/inference-standard.yaml"),
    "Training Heavy      (beta  · burst)":             str(ROOT / "recipes/training-heavy.yaml"),
    "Training Noisy      (beta  · ⚠️ OOMKill)":       str(ROOT / "recipes/training-noisy.yaml"),
    "Data Cleansing      (beta  · light)":             str(ROOT / "recipes/data-cleansing.yaml"),
}
NAMESPACES = ["tenant-alpha", "tenant-beta"]
MAX_HISTORY_PER_POD = 60  # rolling data points per pod
STATE_FILE = Path(__file__).parent / ".cluster_state.json"

# ── State persistence ────────────────────────────────────────────────────────
def _load_state() -> dict:
    """Load persisted setup_done from disk (survives page refreshes)."""
    try:
        if STATE_FILE.exists():
            return json.loads(STATE_FILE.read_text())
    except Exception:
        pass
    return {}

def _save_state():
    """Write current setup_done to disk."""
    try:
        STATE_FILE.write_text(json.dumps(st.session_state.setup_done))
    except Exception:
        pass

def _autodetect_state() -> dict:
    """Probe the actual cluster and return an updated setup_done dict."""
    state = {k: False for k in ["prereqs", "minikube", "built", "manifests", "images", "metrics"]}

    ok_d, _ = _run_cmd(["docker", "info"])
    ok_m, _ = _run_cmd(["minikube", "version"])
    state["prereqs"] = ok_d and ok_m

    ok, out = _run_cmd(["minikube", "status"])
    state["minikube"] = ok and "Running" in out

    if not state["minikube"]:
        return state

    ok, _ = _run_cmd([
        "docker", "image", "inspect",
        "mock-inference:latest", "mock-training:latest", "mock-data-cleansing:latest",
    ])
    state["built"] = ok

    ok, _ = _run_cmd(["kubectl", "get", "namespace", "tenant-alpha", "tenant-beta"])
    state["manifests"] = ok

    ok, out = _run_cmd(["minikube", "image", "ls"])
    state["images"] = ok and all(
        img in out for img in ["mock-inference", "mock-training", "mock-data-cleansing"]
    )

    ok, out = _run_cmd(["minikube", "addons", "list"])
    if ok:
        for line in out.splitlines():
            if "metrics-server" in line:
                state["metrics"] = "enabled" in line.lower()
                break

    return state

# ── Session state ────────────────────────────────────────────────────────────
def _init():
    defaults = {
        "metric_history": [],   # list[dict] with timestamp, pod, namespace, cpu_pct, mem_pct …
        "oom_seen":      set(),
        "oom_log":       [],
        "action_log":    [],
        "setup_done":    {       # tracks which setup steps completed successfully
            "prereqs":   False,
            "minikube":  False,
            "built":     False,
            "manifests": False,
            "images":    False,
            "metrics":   False,
            **_load_state(),    # overlay persisted flags — survives page refresh
        },
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v

_init()
done = st.session_state.setup_done   # shortcut used by sidebar + setup tab

# ── Data helpers ─────────────────────────────────────────────────────────────
@st.cache_resource
def _import_collector():
    from collector import collect_all_namespaces
    return collect_all_namespaces

@st.cache_resource
def _import_deployer():
    from deployer import deploy, purge, list_workloads
    return deploy, purge, list_workloads

@st.cache_resource
def _import_quota():
    from quota_manager import get_quota_status
    return get_quota_status


def collect_metrics():
    """Poll K8s and append snapshot to session_state.metric_history."""
    try:
        collect_all = _import_collector()
        metrics, oom_events = collect_all(NAMESPACES)
    except Exception as exc:
        return [], [], str(exc)

    ts = datetime.now()

    for m in metrics:
        st.session_state.metric_history.append({
            "timestamp": ts,
            "pod":       m.pod,
            "namespace": m.namespace,
            "cpu_pct":   round(m.cpu_percent, 1),
            "mem_pct":   round(m.mem_percent, 1),
            "cpu_used":  m.cpu_used_cores,
            "mem_used":  m.mem_used_bytes,
            "status":    m.status,
            "oom_killed": m.oom_killed,
        })

    # Trim to last MAX_HISTORY_PER_POD rows per pod
    if st.session_state.metric_history:
        df_h = pd.DataFrame(st.session_state.metric_history)
        trimmed = (
            df_h.groupby("pod", group_keys=False)
            .apply(lambda g: g.tail(MAX_HISTORY_PER_POD))
        )
        st.session_state.metric_history = trimmed.to_dict("records")

    # OOM dedup
    for event in oom_events:
        key = f"{event.namespace}/{event.pod}/{event.container}"
        if key not in st.session_state.oom_seen:
            st.session_state.oom_seen.add(key)
            st.session_state.oom_log.append(event)

    return metrics, oom_events, None


def _log(msg: str):
    st.session_state.action_log.append((datetime.now(), msg))


def _run_cmd(cmd: list[str], cwd: str = None) -> tuple[bool, str]:
    """Run a shell command, return (success, combined output)."""
    try:
        result = subprocess.run(
            cmd,
            cwd=cwd or str(ROOT),
            capture_output=True,
            text=True,
            timeout=300,
        )
        output = ((result.stdout or "") + (result.stderr or "")).strip()
        return result.returncode == 0, output
    except subprocess.TimeoutExpired:
        return False, "Timed out after 300s"
    except FileNotFoundError:
        return False, f"Command not found: {cmd[0]}"
    except Exception as exc:
        return False, str(exc)


# ── Sidebar ──────────────────────────────────────────────────────────────────
with st.sidebar:
    st.title("🛡️ KubeAI-Sentry")
    st.caption("Multi-Tenant AI Workload Simulator")
    st.divider()

    # minikube dashboard link
    mk_url = st.text_input(
        "minikube dashboard URL",
        placeholder="http://127.0.0.1:PORT  (run: minikube dashboard --url)",
        help="Run `minikube dashboard --url` in a terminal to get this URL.",
    )
    if mk_url:
        st.link_button("🔗 Open Kubernetes Dashboard", mk_url, use_container_width=True)

    st.divider()
    st.subheader("▶ Deploy Workload")
    deploy_fn, purge_fn, _ = _import_deployer()
    _cluster_ready = done["manifests"] and done["images"]
    if not _cluster_ready:
        st.caption("⚠️ Complete Cluster Setup (steps 1–4) before deploying.")
    for label, path in RECIPES.items():
        if st.button(label, use_container_width=True, key=f"btn_deploy_{path}",
                     disabled=not _cluster_ready):
            with st.spinner("Deploying…"):
                try:
                    r = deploy_fn(path)
                    msg = f"✅ {r['action'].upper()} `{r['name']}` → `{r['namespace']}`"
                    _log(msg)
                    st.success(msg)
                except Exception as exc:
                    st.error(str(exc))

    st.divider()
    st.subheader("💥 Stress / Overload")
    replicas = st.number_input("Replicas", min_value=1, max_value=10, value=3)
    if st.button("Overload: Training Noisy × N", use_container_width=True,
                 disabled=not _cluster_ready):
        with st.spinner("Overloading…"):
            try:
                r = deploy_fn(
                    str(ROOT / "recipes/training-noisy.yaml"),
                    replicas_override=int(replicas),
                )
                msg = f"⚡ OVERLOAD `{r['name']}` × {r['replicas']} in `{r['namespace']}`"
                _log(msg)
                st.warning(msg)
            except Exception as exc:
                st.error(str(exc))

    st.divider()
    st.subheader("🗑 Purge Namespace")
    col_a, col_b = st.columns(2)
    with col_a:
        if st.button("alpha", use_container_width=True):
            try:
                results = purge_fn("tenant-alpha")
                _log(f"🗑 Purged {len(results)} from tenant-alpha")
                st.success(f"Purged {len(results)} deployment(s)")
            except Exception as exc:
                st.error(str(exc))
    with col_b:
        if st.button("beta", use_container_width=True):
            try:
                results = purge_fn("tenant-beta")
                _log(f"🗑 Purged {len(results)} from tenant-beta")
                st.success(f"Purged {len(results)} deployment(s)")
            except Exception as exc:
                st.error(str(exc))

    st.divider()
    auto_refresh = st.checkbox("Auto-refresh", value=False)
    refresh_interval = st.slider("Interval (sec)", min_value=2, max_value=30, value=5)

    if st.session_state.action_log:
        st.divider()
        st.subheader("Action Log")
        for ts_a, msg_a in reversed(st.session_state.action_log[-6:]):
            st.caption(f"`{ts_a.strftime('%H:%M:%S')}` {msg_a}")


# ── Collect data ─────────────────────────────────────────────────────────────
metrics, oom_events, collect_error = collect_metrics()
metrics_ready = any(
    m.cpu_used_cores > 0 or m.mem_used_bytes > 0
    for m in metrics
) if metrics else False


# ── Tabs ─────────────────────────────────────────────────────────────────────
tab_overview, tab_setup, tab_live, tab_charts, tab_quota, tab_workloads, tab_oom = st.tabs([
    "🏠 Overview",
    "🔧 Cluster Setup",
    "📡 Live Metrics",
    "📈 Time-Series Charts",
    "📊 Quota Usage",
    "📋 Workloads",
    "🚨 OOM Events",
])


# ──────────────────────────────────────────────────────────────────────────────
# Tab 0 – Overview
# ──────────────────────────────────────────────────────────────────────────────
with tab_overview:
    st.title("🛡️ KubeAI-Sentry")
    st.markdown(
        "**A multi-tenant Kubernetes simulation of AI workload lifecycles.**  \n"
        "CPU and RAM act as proxies for GPU and VRAM, letting you observe real cluster-level "
        "behaviors — resource isolation, OOMKill detection, and priority-based scheduling — "
        "on a local minikube cluster."
    )

    st.divider()

    col_l, col_r = st.columns(2)

    with col_l:
        st.subheader("What It Simulates")
        st.markdown("""
| Workload | Tenant | Behavior |
|---|---|---|
| `mock-inference` | `tenant-alpha` | Steady 40–60 % CPU, moderate RAM |
| `mock-training` | `tenant-beta` | Burst 80–95 % CPU, high RAM, periodic spikes |
| `mock-data-cleansing` | `tenant-beta` | Light CPU, moderate RAM, simulated I/O wait |
| `training-noisy` | `tenant-beta` | 700 MB alloc in 512 Mi limit → **OOMKill** |
""")

        st.subheader("Three Success Metrics")
        st.markdown("""
1. **Resource Isolation** — `tenant-beta` quota pressure never affects `tenant-alpha`.
2. **OOMKill Detection** — the profiler catches kernel OOM terminations in real time.
3. **Priority Scheduling** — `inference-high` (1000) preempts `training-low` (100) pods.
""")

    with col_r:
        st.subheader("How to Use This Dashboard")
        st.markdown("""
**First-time setup (do once):**

1. Go to **Cluster Setup** and run each step in order (0 → 5).
2. Wait ~60 s after Step 5 for the metrics-server to warm up.

**Running a demo:**

- Use the **sidebar** to deploy workloads or stress the cluster.
- Watch **Live Metrics** for real-time CPU/Memory per pod.
- Check **OOM Events** after deploying `training-noisy` to see the OOMKill.
- Open **Quota Usage** while overloading `tenant-beta` to confirm `tenant-alpha` is isolated.
- **Time-Series Charts** show rolling history (last 60 data points per pod).

**Auto-refresh:**
Toggle *Auto-refresh* in the sidebar and set the interval (2–30 s) for continuous polling.
""")

        st.subheader("Architecture")
        st.markdown("""
```
minikube (Docker driver)
├── tenant-alpha   ResourceQuota · LimitRange · inference-high (1000)
│   └── mock-inference
└── tenant-beta    ResourceQuota · LimitRange · training-low (100)
    ├── mock-training
    ├── mock-training-noisy  ← OOMKill target
    └── mock-data-cleansing
```
""")

    st.divider()
    st.subheader("System Diagram")
    st.image(
        str(Path(__file__).parent / "assets" / "kubeai_diagram.jpg"),
        use_container_width=True,
    )

    st.divider()
    st.caption(
        "Source: [github.com/changju784/kubeai-sentry](https://github.com/changju784/kubeai-sentry)  "
        "· Built with minikube · Kubernetes Python client · Streamlit"
    )


# ──────────────────────────────────────────────────────────────────────────────
# Tab 1 – Cluster Setup
# ──────────────────────────────────────────────────────────────────────────────
with tab_setup:
    st.subheader("Cluster Setup")
    st.caption("Run these steps in order before deploying workloads.")

    # ── State controls ────────────────────────────────────────────────────────
    _col_detect, _col_reset = st.columns([2, 1])
    with _col_detect:
        if st.button("🔍 Auto-detect Cluster State", use_container_width=True,
                     help="Probe minikube/kubectl/docker to update all step badges automatically."):
            with st.spinner("Detecting cluster state…"):
                detected = _autodetect_state()
            st.session_state.setup_done.update(detected)
            _save_state()
            done_count = sum(detected.values())
            st.success(f"Detected {done_count}/6 steps complete.")
            st.rerun()
    with _col_reset:
        if st.button("↺ Reset", use_container_width=True,
                     help="Clear all step badges and the cached state file."):
            for k in st.session_state.setup_done:
                st.session_state.setup_done[k] = False
            _save_state()
            st.rerun()

    # ── Step 0: Check Prerequisites ───────────────────────────────────────────
    st.markdown("---")
    c1, c2 = st.columns([3, 1])
    c1.markdown(
        "**Step 0 — Check Prerequisites**  \n"
        "Verify that Docker Desktop and minikube are installed and reachable."
    )
    status0 = c2.empty()
    status0.markdown("✅ Done" if done["prereqs"] else "⬜ Pending")

    if st.button("Check Docker & Minikube", key="btn_prereqs"):
        with st.spinner("Checking…"):
            ok_docker, out_docker = _run_cmd(["docker", "info", "--format", "Docker is running (version {{.ServerVersion}})"])
            ok_mk,     out_mk     = _run_cmd(["minikube", "version"])
        output = f"$ docker info\n{out_docker}\n\n$ minikube version\n{out_mk}"
        all_ok = ok_docker and ok_mk
        if all_ok:
            st.session_state.setup_done["prereqs"] = True
            _save_state()
            status0.markdown("✅ Done")
            _log("✅ Prerequisites verified")
            st.success("Docker and minikube are available.")
        else:
            msgs = []
            if not ok_docker:
                msgs.append("Docker Desktop does not appear to be running. Start Docker Desktop and try again.")
            if not ok_mk:
                msgs.append("minikube not found. Install minikube and ensure it is on your PATH.")
            st.error("\n".join(msgs))
        with st.expander("Output", expanded=not all_ok):
            st.code(output)

    # ── Step 1: Start Minikube ────────────────────────────────────────────────
    st.markdown("---")
    c1, c2 = st.columns([3, 1])
    c1.markdown(
        "**Step 1 — Start Minikube**  \n"
        "Starts (or resumes) a local minikube cluster using the Docker driver. "
        "Safe to run even if minikube is already running."
    )
    status_mk = c2.empty()
    status_mk.markdown("✅ Done" if done["minikube"] else "⬜ Pending")

    col_mk1, col_mk2 = st.columns(2)
    with col_mk1:
        if st.button("Start Minikube", key="btn_mk_start", use_container_width=True):
            with st.spinner("Starting minikube (this may take a minute)…"):
                ok, out = _run_cmd(["minikube", "start", "--cpus", "2", "--memory", "2048", "--driver=docker"])
            if ok:
                st.session_state.setup_done["minikube"] = True
                _save_state()
                status_mk.markdown("✅ Done")
                _log("✅ minikube started")
                st.success("minikube is running.")
            else:
                st.error("minikube start failed.")
            with st.expander("Output", expanded=not ok):
                st.code(out)
    with col_mk2:
        if st.button("Check minikube Status", key="btn_mk_status", use_container_width=True):
            with st.spinner("Checking…"):
                ok, out = _run_cmd(["minikube", "status"])
            if ok:
                st.session_state.setup_done["minikube"] = True
                _save_state()
                status_mk.markdown("✅ Done")
                st.success("minikube is already running.")
            else:
                st.warning("minikube is not running — click Start Minikube.")
            with st.expander("Output", expanded=True):
                st.code(out)

    # ── Step 2: Build Docker images ───────────────────────────────────────────
    st.markdown("---")
    c1, c2 = st.columns([3, 1])
    c1.markdown(
        "**Step 2 — Build Docker Images**  \n"
        "Builds `mock-inference`, `mock-training`, and `mock-data-cleansing` "
        "from their Dockerfiles in `docker/`."
    )
    status_build = c2.empty()
    status_build.markdown("✅ Done" if done["built"] else "⬜ Pending")

    if st.button("Build Images", key="btn_build"):
        images_to_build = [
            ("mock-inference",      "docker/mock-inference"),
            ("mock-training",       "docker/mock-training"),
            ("mock-data-cleansing", "docker/mock-data-cleansing"),
        ]
        all_ok = True
        outputs = []
        bar = st.progress(0, text="Building images…")
        for i, (tag, ctx) in enumerate(images_to_build):
            bar.progress(i / len(images_to_build), text=f"Building {tag}…")
            ok, out = _run_cmd(["docker", "build", "-t", f"{tag}:latest", ctx])
            outputs.append(f"$ docker build -t {tag}:latest {ctx}\n{out}")
            if not ok:
                all_ok = False
        bar.progress(1.0, text="Done")
        combined = "\n\n".join(outputs)
        if all_ok:
            st.session_state.setup_done["built"] = True
            _save_state()
            status_build.markdown("✅ Done")
            _log("✅ Docker images built")
            st.success("All images built successfully.")
        else:
            st.error("One or more builds failed.")
        with st.expander("Output", expanded=not all_ok):
            st.code(combined)

    # ── Step 3: Apply K8s manifests ──────────────────────────────────────────
    st.markdown("---")
    c1, c2 = st.columns([3, 1])
    c1.markdown("**Step 3 — Apply K8s Manifests**  \nCreates `tenant-alpha` / `tenant-beta` namespaces, ResourceQuotas, LimitRanges, and PriorityClasses.")
    status1 = c2.empty()
    status1.markdown("✅ Done" if done["manifests"] else "⬜ Pending")

    if st.button("Apply Manifests", key="btn_manifests"):
        with st.spinner("Applying manifests…"):
            cmds = [
                ["kubectl", "apply", "-f", "k8s/namespaces/"],
                ["kubectl", "apply", "-f", "k8s/quotas/"],
                ["kubectl", "apply", "-f", "k8s/priority-classes.yaml"],
            ]
            all_ok = True
            outputs = []
            for cmd in cmds:
                ok, out = _run_cmd(cmd)
                outputs.append(f"$ {' '.join(cmd)}\n{out}")
                if not ok:
                    all_ok = False

            combined = "\n\n".join(outputs)
            if all_ok:
                st.session_state.setup_done["manifests"] = True
                _save_state()
                status1.markdown("✅ Done")
                _log("✅ K8s manifests applied")
                st.success("Manifests applied.")
            else:
                st.error("One or more commands failed.")
            with st.expander("Output", expanded=not all_ok):
                st.code(combined)

    # ── Step 2: Load images into minikube ────────────────────────────────────
    st.markdown("---")
    c1, c2 = st.columns([3, 1])
    c1.markdown("**Step 4 — Load Images into Minikube**  \nCopies `mock-inference`, `mock-training`, `mock-data-cleansing` from host Docker into minikube's internal image store.")
    status2 = c2.empty()
    status2.markdown("✅ Done" if done["images"] else "⬜ Pending")

    if st.button("Load Images", key="btn_images"):
        images = ["mock-inference:latest", "mock-training:latest", "mock-data-cleansing:latest"]
        all_ok = True
        outputs = []
        bars = st.progress(0, text="Loading images…")
        for i, img in enumerate(images):
            bars.progress((i) / len(images), text=f"Loading {img}…")
            ok, out = _run_cmd(["minikube", "image", "load", img])
            outputs.append(f"$ minikube image load {img}\n{out}")
            if not ok:
                all_ok = False
        bars.progress(1.0, text="Done")

        combined = "\n\n".join(outputs)
        if all_ok:
            st.session_state.setup_done["images"] = True
            _save_state()
            status2.markdown("✅ Done")
            _log("✅ Images loaded into minikube")
            st.success("All images loaded.")
        else:
            st.error("One or more images failed to load.")
        with st.expander("Output", expanded=not all_ok):
            st.code(combined)

    # ── Step 3: Enable metrics-server ────────────────────────────────────────
    st.markdown("---")
    c1, c2 = st.columns([3, 1])
    c1.markdown("**Step 5 — Enable Metrics Server**  \nRequired for live CPU/Memory data in the profiler. Takes ~60 s to warm up after enabling.")
    status3 = c2.empty()
    status3.markdown("✅ Done" if done["metrics"] else "⬜ Pending")

    if st.button("Enable Metrics Server", key="btn_metrics"):
        with st.spinner("Enabling metrics-server addon…"):
            ok, out = _run_cmd(["minikube", "addons", "enable", "metrics-server"])
        if ok:
            st.session_state.setup_done["metrics"] = True
            _save_state()
            status3.markdown("✅ Done")
            _log("✅ metrics-server enabled")
            st.success("Metrics server enabled. Allow ~60 s for data to appear.")
        else:
            st.error("Failed to enable metrics-server.")
        with st.expander("Output", expanded=not ok):
            st.code(out)

    # ── Step 4: Verify cluster ───────────────────────────────────────────────
    st.markdown("---")
    st.markdown("**Step 6 — Verify Cluster**  \nQuick sanity check: nodes, namespaces, and loaded images.")
    if st.button("Run Cluster Check", key="btn_verify"):
        with st.spinner("Checking…"):
            checks = {
                "Nodes":       ["kubectl", "get", "nodes"],
                "Namespaces":  ["kubectl", "get", "namespaces"],
                "Minikube images": ["minikube", "image", "ls"],
            }
            output_parts = []
            for label, cmd in checks.items():
                ok, out = _run_cmd(cmd)
                output_parts.append(f"── {label} ──\n{out}")
            st.code("\n\n".join(output_parts))


# ──────────────────────────────────────────────────────────────────────────────
# Tab 1 – Live Metrics
# ──────────────────────────────────────────────────────────────────────────────
with tab_live:
    st.subheader("Pod Resource Utilization")

    if collect_error:
        st.error(f"Kubernetes API error: {collect_error}")
    elif not metrics:
        st.info("No pods found. Deploy a workload from the sidebar to get started.")
    else:
        if not metrics_ready:
            st.warning("⏳ Waiting for metrics server… (CPU/Memory values appear after ~60 s)")

        rows = []
        for m in sorted(metrics, key=lambda x: (0 if x.oom_killed else 1, x.namespace, x.pod)):
            rows.append({
                "Pod":      m.pod,
                "Namespace": m.namespace,
                "CPU Used":  f"{m.cpu_used_cores * 1000:.0f}m" if metrics_ready else "—",
                "CPU %":     m.cpu_percent if metrics_ready else None,
                "Mem Used":  f"{m.mem_used_bytes / 1024**2:.1f} Mi" if metrics_ready else "—",
                "Mem %":     m.mem_percent if metrics_ready else None,
                "Status":    m.status,
            })

        df_live = pd.DataFrame(rows)

        def _row_bg(row):
            s = row["Status"].lower()
            if "oomkill" in s:
                return ["background-color: #4a0000; color: #ff9999"] * len(row)
            if "restarted" in s:
                return ["background-color: #3d2a00; color: #ffcc66"] * len(row)
            return [""] * len(row)

        def _pct_color(val):
            if val is None or not isinstance(val, (int, float)):
                return ""
            if val >= 85:
                return "color: #ff4b4b; font-weight: bold"
            if val >= 60:
                return "color: #ffa500"
            return "color: #00cc44"

        styled_live = (
            df_live.style
            .apply(_row_bg, axis=1)
            .map(_pct_color, subset=["CPU %", "Mem %"])
            .format({
                "CPU %": lambda v: f"{v:.1f}%" if isinstance(v, (int, float)) else "—",
                "Mem %": lambda v: f"{v:.1f}%" if isinstance(v, (int, float)) else "—",
            })
        )
        st.dataframe(styled_live, use_container_width=True, hide_index=True)

        # Summary cards
        if metrics_ready:
            st.divider()
            c1, c2, c3, c4 = st.columns(4)
            running   = sum(1 for m in metrics if "running" in m.status.lower())
            oom_count = sum(1 for m in metrics if m.oom_killed)
            avg_cpu   = sum(m.cpu_percent for m in metrics) / len(metrics)
            avg_mem   = sum(m.mem_percent for m in metrics) / len(metrics)
            c1.metric("Running Pods", running)
            c2.metric(
                "OOMKilled",
                oom_count,
                delta=f"+{oom_count}" if oom_count else None,
                delta_color="inverse",
            )
            c3.metric("Avg CPU %", f"{avg_cpu:.1f}%")
            c4.metric("Avg Mem %", f"{avg_mem:.1f}%")


# ──────────────────────────────────────────────────────────────────────────────
# Tab 2 – Time-Series Charts
# ──────────────────────────────────────────────────────────────────────────────
with tab_charts:
    st.subheader("Resource Usage Over Time")

    if not st.session_state.metric_history:
        st.info("No history yet — charts populate as data accumulates.")
    else:
        hist_df = pd.DataFrame(st.session_state.metric_history)
        all_pods = sorted(hist_df["pod"].unique().tolist())

        selected = st.multiselect(
            "Filter Pods",
            options=all_pods,
            default=all_pods,
            key="chart_filter",
        )
        filtered_df = hist_df[hist_df["pod"].isin(selected)]

        col_cpu, col_mem = st.columns(2)

        def _ns_color(ns: str) -> str:
            return "#4da6ff" if "alpha" in ns else "#ff9944"

        with col_cpu:
            st.markdown("**CPU % over Time**")
            fig_cpu = go.Figure()
            for pod in selected:
                pod_df = filtered_df[filtered_df["pod"] == pod].sort_values("timestamp")
                if pod_df.empty:
                    continue
                ns = pod_df["namespace"].iloc[0]
                fig_cpu.add_trace(go.Scatter(
                    x=pod_df["timestamp"],
                    y=pod_df["cpu_pct"],
                    name=pod,
                    mode="lines",
                    line=dict(color=_ns_color(ns), width=2),
                    hovertemplate="%{y:.1f}%<extra>" + pod + "</extra>",
                ))
            fig_cpu.add_hline(
                y=85, line_dash="dash", line_color="red", opacity=0.6,
                annotation_text="85% alert",
            )
            fig_cpu.update_layout(
                xaxis_title="Time",
                yaxis_title="CPU %",
                yaxis_range=[0, 105],
                height=330,
                margin=dict(l=0, r=0, t=10, b=0),
                legend=dict(orientation="h", yanchor="bottom", y=1.02),
                paper_bgcolor="rgba(0,0,0,0)",
                plot_bgcolor="rgba(0,0,0,0)",
            )
            st.plotly_chart(fig_cpu, use_container_width=True)

        with col_mem:
            st.markdown("**Memory % over Time**")
            fig_mem = go.Figure()
            for pod in selected:
                pod_df = filtered_df[filtered_df["pod"] == pod].sort_values("timestamp")
                if pod_df.empty:
                    continue
                ns = pod_df["namespace"].iloc[0]
                fig_mem.add_trace(go.Scatter(
                    x=pod_df["timestamp"],
                    y=pod_df["mem_pct"],
                    name=pod,
                    mode="lines",
                    line=dict(color=_ns_color(ns), width=2),
                    hovertemplate="%{y:.1f}%<extra>" + pod + "</extra>",
                ))
            fig_mem.add_hline(
                y=85, line_dash="dash", line_color="red", opacity=0.6,
                annotation_text="85% alert",
            )
            fig_mem.update_layout(
                xaxis_title="Time",
                yaxis_title="Memory %",
                yaxis_range=[0, 105],
                height=330,
                margin=dict(l=0, r=0, t=10, b=0),
                legend=dict(orientation="h", yanchor="bottom", y=1.02),
                paper_bgcolor="rgba(0,0,0,0)",
                plot_bgcolor="rgba(0,0,0,0)",
            )
            st.plotly_chart(fig_mem, use_container_width=True)

        st.caption(
            "🔵 Blue = tenant-alpha   🟠 Orange = tenant-beta   "
            f"| Keeping last {MAX_HISTORY_PER_POD} points per pod"
        )


# ──────────────────────────────────────────────────────────────────────────────
# Tab 3 – Quota Usage
# ──────────────────────────────────────────────────────────────────────────────
with tab_quota:
    st.subheader("Namespace ResourceQuota")
    try:
        get_quota = _import_quota()
        for ns in NAMESPACES:
            st.markdown(f"#### {ns}")
            rows = get_quota(ns)
            if not rows:
                st.caption("No quota found.")
                continue

            for row in rows:
                pct = row["percent"]
                icon = "🟢" if pct < 70 else ("🟡" if pct < 90 else "🔴")
                label = (
                    f"{icon} **{row['resource']}** — "
                    f"{row['used']} / {row['hard']}  ({pct:.1f}%)"
                )
                st.progress(min(pct / 100.0, 1.0), text=label)
    except Exception as exc:
        st.error(f"Could not fetch quota: {exc}")


# ──────────────────────────────────────────────────────────────────────────────
# Tab 4 – Workloads
# ──────────────────────────────────────────────────────────────────────────────
with tab_workloads:
    st.subheader("Active Deployments")
    try:
        _, _, list_fn = _import_deployer()
        workloads = list_fn("all")
        if not workloads:
            st.info("No deployments found.")
        else:
            df_w = pd.DataFrame(workloads)[
                ["name", "namespace", "workload_type", "priority_class", "replicas", "ready", "status"]
            ]
            df_w.columns = ["Name", "Namespace", "Type", "Priority Class", "Replicas", "Ready", "Status"]

            def _status_color(val):
                low = val.lower()
                if low == "running":
                    return "color: #00cc44; font-weight: bold"
                if low == "pending":
                    return "color: #ffa500"
                return "color: #ff4b4b"

            st.dataframe(
                df_w.style.map(_status_color, subset=["Status"]),
                use_container_width=True,
                hide_index=True,
            )
    except Exception as exc:
        st.error(f"Could not list workloads: {exc}")


# ──────────────────────────────────────────────────────────────────────────────
# Tab 5 – OOM Events
# ──────────────────────────────────────────────────────────────────────────────
with tab_oom:
    st.subheader("🚨 OOMKill Events")
    if not st.session_state.oom_log:
        st.success("✅ No OOMKill events detected.")
    else:
        oom_rows = [
            {
                "Time":      e.timestamp.strftime("%H:%M:%S") if hasattr(e.timestamp, "strftime") else str(e.timestamp),
                "Pod":       e.pod,
                "Namespace": e.namespace,
                "Container": e.container,
                "Exit Code": e.exit_code,
                "Message":   getattr(e, "message", "OOMKilled"),
            }
            for e in reversed(st.session_state.oom_log)
        ]
        df_oom = pd.DataFrame(oom_rows)
        st.dataframe(
            df_oom.style.set_properties(
                **{"background-color": "#4a0000", "color": "#ff9999"},
                subset=pd.IndexSlice[:, ["Pod", "Exit Code"]],
            ),
            use_container_width=True,
            hide_index=True,
        )
        if st.button("Clear OOM log"):
            st.session_state.oom_log.clear()
            st.session_state.oom_seen.clear()
            st.rerun()


# ── Auto-refresh ─────────────────────────────────────────────────────────────
if auto_refresh:
    time.sleep(refresh_interval)
    st.rerun()
