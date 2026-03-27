"""
Mock Inference Workload
Simulates steady AI inference: 20-30% CPU, light RAM.
Reads env vars: LOAD_PROFILE, MEMORY_TARGET_MB, CPU_CORES, DURATION_SECONDS
"""
import os
import time
import threading
import math

LOAD_PROFILE = os.environ.get("LOAD_PROFILE", "steady")
MEMORY_TARGET_MB = int(os.environ.get("MEMORY_TARGET_MB", "64"))
CPU_CORES = float(os.environ.get("CPU_CORES", "0.2"))
DURATION_SECONDS = int(os.environ.get("DURATION_SECONDS", "3600"))

# Keep memory allocation alive at module level
_memory_block = None


def allocate_memory(target_mb: int):
    """Allocate and hold a bytearray to simulate memory usage."""
    global _memory_block
    try:
        _memory_block = bytearray(target_mb * 1024 * 1024)
        # Touch pages to ensure physical allocation
        for i in range(0, len(_memory_block), 4096):
            _memory_block[i] = i % 256
        print(f"[inference] Allocated {target_mb}MB of memory")
    except MemoryError:
        print(f"[inference] MemoryError allocating {target_mb}MB – OOMKill likely imminent")


def cpu_worker(stop_event: threading.Event, target_fraction: float):
    """Spin on math to consume CPU proportional to target_fraction (0.0–1.0)."""
    cycle_ms = 100  # 100ms cycle
    work_ms = cycle_ms * target_fraction
    sleep_ms = cycle_ms - work_ms

    while not stop_event.is_set():
        deadline = time.monotonic() + work_ms / 1000.0
        while time.monotonic() < deadline:
            # Arithmetic busy-loop to burn CPU
            _ = math.sqrt(sum(i * i for i in range(100)))
        if sleep_ms > 0:
            time.sleep(sleep_ms / 1000.0)


def run_steady(stop_event: threading.Event):
    """Steady load: constant CPU at target level."""
    print(f"[inference] Running STEADY profile at {CPU_CORES} cores for {DURATION_SECONDS}s")
    num_threads = max(1, round(CPU_CORES))
    per_thread = CPU_CORES / num_threads

    workers = []
    for _ in range(num_threads):
        t = threading.Thread(target=cpu_worker, args=(stop_event, min(per_thread, 0.6)), daemon=True)
        t.start()
        workers.append(t)

    stop_event.wait(timeout=DURATION_SECONDS)
    stop_event.set()
    for t in workers:
        t.join(timeout=2)


def run_burst(stop_event: threading.Event):
    """Burst load: alternate between high and low CPU every 30s."""
    print(f"[inference] Running BURST profile for {DURATION_SECONDS}s")
    high_fraction = min(CPU_CORES, 0.95)
    low_fraction = 0.1
    burst_interval = 30  # seconds per phase

    start = time.monotonic()
    while not stop_event.is_set() and (time.monotonic() - start) < DURATION_SECONDS:
        phase_stop = threading.Event()
        fraction = high_fraction if int((time.monotonic() - start) / burst_interval) % 2 == 0 else low_fraction
        num_threads = max(1, round(CPU_CORES))
        per_thread = fraction / num_threads

        workers = []
        for _ in range(num_threads):
            t = threading.Thread(target=cpu_worker, args=(phase_stop, per_thread), daemon=True)
            t.start()
            workers.append(t)

        time.sleep(min(burst_interval, DURATION_SECONDS - (time.monotonic() - start)))
        phase_stop.set()
        for t in workers:
            t.join(timeout=2)

    stop_event.set()


def main():
    print(f"[inference] Starting: profile={LOAD_PROFILE}, mem={MEMORY_TARGET_MB}MB, "
          f"cpu={CPU_CORES}, duration={DURATION_SECONDS}s")

    allocate_memory(MEMORY_TARGET_MB)

    stop_event = threading.Event()

    if LOAD_PROFILE == "burst":
        run_burst(stop_event)
    else:
        run_steady(stop_event)

    print("[inference] Workload complete.")


if __name__ == "__main__":
    main()
