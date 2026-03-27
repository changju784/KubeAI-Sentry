"""
Mock Data Cleansing Workload
Simulates light data pipeline: low CPU, moderate RAM, simulated I/O wait.
Reads env vars: LOAD_PROFILE, MEMORY_TARGET_MB, CPU_CORES, DURATION_SECONDS
"""
import os
import time
import threading
import math
import random

LOAD_PROFILE = os.environ.get("LOAD_PROFILE", "steady")
MEMORY_TARGET_MB = int(os.environ.get("MEMORY_TARGET_MB", "32"))
CPU_CORES = float(os.environ.get("CPU_CORES", "0.1"))
DURATION_SECONDS = int(os.environ.get("DURATION_SECONDS", "3600"))

_memory_block = None
_working_set = []  # Simulate data chunks in memory


def allocate_memory(target_mb: int):
    """Allocate memory to simulate dataset loaded into RAM."""
    global _memory_block, _working_set
    try:
        print(f"[data-cleansing] Loading dataset: {target_mb}MB")
        _memory_block = bytearray(target_mb * 1024 * 1024)
        # Simulate chunked data access pattern
        chunk_size = 1024 * 1024  # 1MB chunks
        for offset in range(0, len(_memory_block), chunk_size):
            end = min(offset + chunk_size, len(_memory_block))
            for i in range(offset, end, 4096):
                _memory_block[i] = random.randint(0, 255)
        print(f"[data-cleansing] Dataset loaded: {target_mb}MB")
    except MemoryError:
        print(f"[data-cleansing] MemoryError loading {target_mb}MB dataset")


def cpu_worker(stop_event: threading.Event, target_fraction: float):
    """Light CPU work simulating data transformation + frequent I/O waits."""
    cycle_ms = 200  # longer cycle for I/O-bound pattern
    work_ms = cycle_ms * target_fraction
    io_wait_ms = cycle_ms - work_ms  # simulate I/O wait

    while not stop_event.is_set():
        # Data processing burst (regex matching, type conversion simulation)
        deadline = time.monotonic() + work_ms / 1000.0
        while time.monotonic() < deadline:
            _ = math.log(sum(abs(math.sin(i)) for i in range(50)) + 1)

        # Simulate I/O wait (disk read, network call to data source)
        if io_wait_ms > 0:
            time.sleep(io_wait_ms / 1000.0)


def run_steady(stop_event: threading.Event):
    """Steady low-CPU load with regular I/O waits."""
    print(f"[data-cleansing] Running STEADY profile at {CPU_CORES} cores for {DURATION_SECONDS}s")
    num_threads = max(1, round(CPU_CORES * 2))  # more threads, lower CPU each
    per_thread = CPU_CORES / num_threads

    workers = []
    for i in range(num_threads):
        t = threading.Thread(target=cpu_worker, args=(stop_event, per_thread), daemon=True)
        t.start()
        workers.append(t)

    # Simulate periodic batch completions
    start = time.monotonic()
    batch_num = 0
    while not stop_event.is_set() and (time.monotonic() - start) < DURATION_SECONDS:
        time.sleep(10)
        batch_num += 1
        print(f"[data-cleansing] Processed batch {batch_num}")

    stop_event.set()
    for t in workers:
        t.join(timeout=2)


def run_burst(stop_event: threading.Event):
    """Burst: occasional CPU spikes during data validation, mostly idle."""
    print(f"[data-cleansing] Running BURST profile for {DURATION_SECONDS}s")
    burst_interval = 20  # seconds between bursts
    burst_duration = 5   # seconds of high CPU

    start = time.monotonic()

    while not stop_event.is_set() and (time.monotonic() - start) < DURATION_SECONDS:
        # Idle phase (simulating I/O-bound data fetch)
        idle_time = burst_interval - burst_duration
        print(f"[data-cleansing] Idle phase ({idle_time}s I/O wait)...")
        time.sleep(min(idle_time, DURATION_SECONDS - (time.monotonic() - start)))

        if stop_event.is_set():
            break

        # CPU burst (data validation/schema check)
        print(f"[data-cleansing] Validation burst ({burst_duration}s CPU spike)...")
        burst_stop = threading.Event()
        num_threads = max(1, round(CPU_CORES))
        per_thread = min(CPU_CORES / num_threads, 0.8)

        workers = []
        for _ in range(num_threads):
            t = threading.Thread(target=cpu_worker, args=(burst_stop, per_thread), daemon=True)
            t.start()
            workers.append(t)

        time.sleep(min(burst_duration, DURATION_SECONDS - (time.monotonic() - start)))
        burst_stop.set()
        for t in workers:
            t.join(timeout=2)

    stop_event.set()


def main():
    print(f"[data-cleansing] Starting: profile={LOAD_PROFILE}, mem={MEMORY_TARGET_MB}MB, "
          f"cpu={CPU_CORES}, duration={DURATION_SECONDS}s")

    allocate_memory(MEMORY_TARGET_MB)

    stop_event = threading.Event()

    if LOAD_PROFILE == "burst":
        run_burst(stop_event)
    else:
        run_steady(stop_event)

    print("[data-cleansing] Workload complete.")


if __name__ == "__main__":
    main()
