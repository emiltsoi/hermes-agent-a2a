"""Runtime registry for cancelable Hermes worker subprocesses."""

from __future__ import annotations

import subprocess
from threading import Lock

_lock = Lock()
_processes: dict[str, subprocess.Popen] = {}


def register_worker(task_id: str, process: subprocess.Popen) -> None:
    with _lock:
        _processes[task_id] = process


def unregister_worker(task_id: str) -> None:
    with _lock:
        _processes.pop(task_id, None)


def cancel_worker(task_id: str, timeout: float = 3.0) -> bool:
    with _lock:
        process = _processes.get(task_id)
    if process is None or process.poll() is not None:
        return False
    process.terminate()
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=timeout)
    return True
