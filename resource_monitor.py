"""Lightweight CPU monitor for Raspberry Pi – disables video when load is high."""

import threading

_cpu_percent = 0.0
_lock = threading.Lock()
_running = False

CPU_VIDEO_THRESHOLD = 75.0
SAMPLE_INTERVAL = 5


def get_cpu_percent():
    with _lock:
        return _cpu_percent


def should_disable_video():
    return get_cpu_percent() > CPU_VIDEO_THRESHOLD


def set_threshold(value):
    global CPU_VIDEO_THRESHOLD
    CPU_VIDEO_THRESHOLD = max(10.0, min(100.0, float(value)))


def _monitor_loop():
    global _cpu_percent
    try:
        import psutil
    except ImportError:
        print("psutil not installed – CPU monitoring disabled")
        return

    while _running:
        sample = psutil.cpu_percent(interval=SAMPLE_INTERVAL)
        with _lock:
            _cpu_percent = sample


def start():
    global _running
    if _running:
        return
    _running = True
    t = threading.Thread(target=_monitor_loop, daemon=True)
    t.start()
    print("CPU monitor started (threshold: {:.0f}%)".format(CPU_VIDEO_THRESHOLD))


def stop():
    global _running
    _running = False
