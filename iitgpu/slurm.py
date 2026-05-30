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
class NodeStats:
    state: str
    cpu_load: float
    cpu_total: int
    cpu_alloc: int
    mem_total_mb: int
    mem_alloc_mb: int
    gpu_total: int
    gpu_alloc: int


@dataclass
class QueueEntry:
    job_id: str
    name: str
    state: str
    partition: str
    time_used: str
    nodes: int
    user: str = "?"
    time_limit: str = "N/A"


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
    # All jobs are submitted via `sudo -u daham sbatch`; query as daham to see
    # them all.  Include user (%u) and time-limit (%l) for the dashboard.
    cmd = ["sudo", "-u", "daham", "squeue", "--noheader",
           "--format=%i %j %u %T %P %M %l %D", "-u", "daham"]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        entries = []
        for line in result.stdout.splitlines():
            parts = line.split()
            if len(parts) < 8:
                continue
            entries.append(QueueEntry(
                job_id=parts[0], name=parts[1], user=parts[2],
                state=parts[3], partition=parts[4],
                time_used=parts[5], time_limit=parts[6],
                nodes=int(parts[7]) if parts[7].isdigit() else 1,
            ))
        return entries
    except (OSError, subprocess.TimeoutExpired):
        return []


def get_node_stats(node_name: str = "iit-MS-7E06") -> NodeStats | None:
    """Return live CPU/memory/GPU stats for a cluster node via scontrol."""
    try:
        result = subprocess.run(
            ["scontrol", "show", "node", node_name, "--oneliner"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            return None
        d: dict[str, str] = {}
        for token in result.stdout.split():
            if "=" in token:
                k, _, v = token.partition("=")
                d[k] = v

        def _i(key: str, default: int = 0) -> int:
            try:
                return int(d.get(key, default))
            except (ValueError, TypeError):
                return default

        def _f(key: str, default: float = 0.0) -> float:
            try:
                return float(d.get(key, default))
            except (ValueError, TypeError):
                return default

        gpu_total = 0
        for part in d.get("Gres", "").split(","):
            if part.startswith("gpu:"):
                try:
                    gpu_total += int(part.rstrip(")").split(":")[-1].split("(")[0])
                except (ValueError, IndexError):
                    gpu_total += 1

        gpu_alloc = 0
        for part in d.get("GresUsed", "").split(","):
            if part.startswith("gpu:"):
                try:
                    gpu_alloc += int(part.split(":")[1].split("(")[0])
                except (ValueError, IndexError):
                    pass

        return NodeStats(
            state=d.get("State", "?").split("+")[0],
            cpu_load=_f("CPULoad"),
            cpu_total=_i("CPUTot"),
            cpu_alloc=_i("CPUAlloc"),
            mem_total_mb=_i("RealMemory"),
            mem_alloc_mb=_i("AllocMem"),
            gpu_total=gpu_total,
            gpu_alloc=gpu_alloc,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


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
        err_file = f.with_suffix(".err")
        try:
            failed = err_file.exists() and err_file.stat().st_size > 0
        except OSError:
            failed = False
        state = "FAILED" if failed else "COMPLETED"
        result.append(QueueEntry(job_id, name, state, "gpu", "-", 1))
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
