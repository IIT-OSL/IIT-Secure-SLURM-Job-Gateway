from __future__ import annotations
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Partition:
    name: str
    state: str
    nodes: int
    gpus_per_node: int


@dataclass
class QueueEntry:
    job_id: str
    name: str
    state: str
    partition: str
    time_used: str
    nodes: int


_DEMO_PARTITIONS = [
    Partition("gpu-short", "up", 4, 4),
    Partition("gpu-long", "up", 8, 8),
    Partition("gpu-debug", "up", 1, 2),
]

_DEMO_QUEUE: list[QueueEntry] = []
_DEMO_COUNTER = [1000]


def _demo_mode() -> bool:
    return os.environ.get("DEMO_MODE", "0") == "1"


def get_partitions() -> list[Partition]:
    if _demo_mode():
        return list(_DEMO_PARTITIONS)
    try:
        result = subprocess.run(
            ["sinfo", "--noheader", "--format=%P %a %D %G"],
            capture_output=True, text=True, timeout=10,
        )
        partitions = []
        for line in result.stdout.splitlines():
            parts = line.split()
            if len(parts) < 4:
                continue
            name = parts[0].rstrip("*")
            state = parts[1]
            nodes = int(parts[2]) if parts[2].isdigit() else 0
            gpus = 0
            if "gpu:" in parts[3]:
                try:
                    gpus = int(parts[3].split("gpu:")[1].split("(")[0])
                except (ValueError, IndexError):
                    gpus = 0
            partitions.append(Partition(name, state, nodes, gpus))
        return partitions
    except (OSError, subprocess.TimeoutExpired):
        return []


def submit_job(script_path: str) -> tuple[bool, str]:
    if _demo_mode():
        _DEMO_COUNTER[0] += 1
        job_id = str(_DEMO_COUNTER[0])
        _DEMO_QUEUE.append(QueueEntry(job_id, "demo_job", "PENDING", "gpu-short", "0:00", 1))
        return True, job_id
    try:
        result = subprocess.run(
            ["sudo", "-u", "daham", "sbatch", script_path],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0:
            parts = result.stdout.strip().split()
            return True, parts[-1] if parts else "unknown"
        return False, result.stderr.strip()
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)


def queue(user: str | None = None) -> list[QueueEntry]:
    if _demo_mode():
        return list(_DEMO_QUEUE)
    # Jobs are always submitted via `sudo -u daham sbatch`, so SLURM owns them
    # as daham.  Query as daham so the queue reflects what we actually submitted.
    cmd = ["sudo", "-u", "daham", "squeue", "--noheader", "--format=%i %j %T %P %M %D", "-u", "daham"]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        entries = []
        for line in result.stdout.splitlines():
            parts = line.split()
            if len(parts) < 6:
                continue
            entries.append(QueueEntry(parts[0], parts[1], parts[2], parts[3], parts[4], int(parts[5])))
        return entries
    except (OSError, subprocess.TimeoutExpired):
        return []


def recent_jobs(search_root: str, limit: int = 2) -> list[QueueEntry]:
    """Return up to `limit` recently completed jobs by scanning output files."""
    try:
        out_files = sorted(
            Path(search_root).rglob("slurm-*.out"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )[:limit]
    except OSError:
        return []
    result = []
    for f in out_files:
        job_id = f.stem[len("slurm-"):]
        name = f.parent.name
        result.append(QueueEntry(job_id, name, "COMPLETED", "gpu", "-", 1))
    return result


def cancel(job_id: str) -> tuple[bool, str]:
    if _demo_mode():
        before = len(_DEMO_QUEUE)
        _DEMO_QUEUE[:] = [e for e in _DEMO_QUEUE if e.job_id != job_id]
        if len(_DEMO_QUEUE) < before:
            return True, f"Job {job_id} cancelled"
        return False, f"Job {job_id} not found"
    try:
        result = subprocess.run(
            ["sudo", "-u", "daham", "scancel", job_id],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            return True, f"Job {job_id} cancelled"
        return False, result.stderr.strip()
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
