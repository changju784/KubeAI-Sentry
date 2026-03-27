"""
Mock Training Workload
Simulates AI training: burst 50-70% CPU, moderate RAM, periodic spikes.
Reads env vars: LOAD_PROFILE, MEMORY_TARGET_MB, CPU_CORES, DURATION_SECONDS
"""
import os
import time
import threading
import math

LOAD_PROFILE = os.environ.get("LOAD_PROFILE", "burst")
MEMORY_TARGET_MB = int(os.environ.get("MEMORY_TARGET_MB", "128"))
CPU_CORES = float(os.environ.get("CPU_CORES", "0.3"))
DURATION_SECONDS = int(os.environ.get("DURATION_SECONDS", "3600"))

_memory_block = None


def allocate_memory(target_mb: int):
    """Allocate and hold a bytearray to simulate model parameter memory.
    If target_mb exceeds container limit, the kernel will OOMKill the process.
    """
    global _memory_block
    try:
        print(f"[training] Allocating {target_mb}MB of memory...")
        _memory_block = bytearray(target_mb * 1024 * 1024)
        # Touch every page to force physical allocation (triggers OOMKill if over limit)
        for i in range(0, len(_memory_block), 4096):
            _memory_block[i] = i % 256
        print(f"[training] Allocated {target_mb}MB of memory")
    except MemoryError:
        print(f"[training] MemoryError allocating {target_mb}MB – OOMKill likely imminent")


def cpu_worker(stop_event: threading.Event, target_fraction: float):
    """Spin on math to consume CPU proportional to target_fraction (0.0–1.0)."""
    cycle_ms = 100
    work_ms = cycle_ms * target_fraction
    sleep_ms = cycle_ms - work_ms

    while not stop_event.is_set():
        deadline = time.monotonic() + work_ms / 1000.0
        while time.monotonic() < deadline:
            # Simulate gradient computation with heavy math
            _ = math.sqrt(sum(i * i * math.sin(i) for i in range(200)))
        if sleep_ms > 0:
            time.sleep(sleep_ms / 1000.0)


def run_burst(stop_event: threading.Event):
    """Burst load: alternate between 95% and 20% CPU every 30s (training epochs)."""
    print(f"[training] Running BURST profile at up to {CPU_CORES} cores for {DURATION_SECONDS}s")
    high_fraction = min(CPU_CORES, 0.95)
    low_fraction = 0.2  # checkpoint saving phase
    burst_interval = 30

    start = time.monotonic()
    phase_num = 0

    while not stop_event.is_set() and (time.monotonic() - start) < DURATION_SECONDS:
        elapsed = time.monotonic() - start
        is_high = (int(elapsed / burst_interval) % 2) == 0
        fraction = high_fraction if is_high else low_fraction
        phase_label = "TRAINING" if is_high else "CHECKPOINT"

        print(f"[training] Phase {phase_num}: {phase_label} ({fraction:.0%} CPU)")

        phase_stop = threading.Event()
        num_threads = max(1, round(CPU_CORES))
        per_thread = fraction / num_threads

        workers = []
        for _ in range(num_threads):
            t = threading.Thread(target=cpu_worker, args=(phase_stop, per_thread), daemon=True)
            t.start()
            workers.append(t)

        remaining = DURATION_SECONDS - (time.monotonic() - start)
        time.sleep(min(burst_interval, remaining))
        phase_stop.set()
        for t in workers:
            t.join(timeout=2)

        phase_num += 1

    stop_event.set()


def run_steady(stop_event: threading.Event):
    """Steady load: constant high CPU."""
    print(f"[training] Running STEADY profile at {CPU_CORES} cores for {DURATION_SECONDS}s")
    num_threads = max(1, round(CPU_CORES))
    per_thread = CPU_CORES / num_threads

    workers = []
    for _ in range(num_threads):
        t = threading.Thread(target=cpu_worker, args=(stop_event, min(per_thread, 0.95)), daemon=True)
        t.start()
        workers.append(t)

    stop_event.wait(timeout=DURATION_SECONDS)
    stop_event.set()
    for t in workers:
        t.join(timeout=2)


def main():
    print(f"[training] Starting: profile={LOAD_PROFILE}, mem={MEMORY_TARGET_MB}MB, "
          f"cpu={CPU_CORES}, duration={DURATION_SECONDS}s")

    # Allocate memory first – if MEMORY_TARGET_MB > container limit, OOMKill happens here
    allocate_memory(MEMORY_TARGET_MB)

    stop_event = threading.Event()

    if LOAD_PROFILE == "steady":
        run_steady(stop_event)
    else:
        run_burst(stop_event)

    print("[training] Workload complete.")


if __name__ == "__main__":
    main()
