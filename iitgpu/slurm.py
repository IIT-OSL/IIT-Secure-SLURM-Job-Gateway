from __future__ import annotations
import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

_GPU_STATS_FILE = "/shared/.gpu_stats.json"
_GPU_STATS_MAX_AGE = 10   # seconds — stale if older than this


@dataclass
class Partition:
    name: str
    state: str
    nodes: int
    gpus_per_node: int


@dataclass
class NodeStats:
    # SLURM allocation data (from scontrol)
    state: str
    cpu_load: float
    cpu_total: int
    cpu_alloc: int
    mem_total_mb: int
    mem_alloc_mb: int
    gpu_total: int
    gpu_alloc: int
    # Actual utilization data (from nvidia-smi via stats writer)
    gpu_util: int = 0
    gpu_mem_used_mb: int = 0
    gpu_mem_total_mb: int = 0
    gpu_temp: int = 0
    gpu_power_w: float = 0.0
    cpu_util: int = 0
    mem_used_mb: int = 0
    live_stats: bool = False   # True when fields above are from the stats file


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


def _read_gpu_stats_file() -> dict | None:
    """Read /shared/.gpu_stats.json written by the iit-gpu-stats-writer daemon."""
    try:
        p = Path(_GPU_STATS_FILE)
        if not p.exists():
            return None
        age = time.time() - p.stat().st_mtime
        if age > _GPU_STATS_MAX_AGE:
            return None   # stale — daemon probably died
        return json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def get_node_stats(node_name: str = "iit-MS-7E06") -> NodeStats | None:
    """Return live stats: SLURM allocation from scontrol + actual utilization from stats file."""
    try:
        result = subprocess.run(
            ["sudo", "-u", "daham", "scontrol", "show", "node", node_name, "--oneliner"],
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
        mem_alloc_mb = 0
        for item in d.get("AllocTRES", "").split(","):
            if item.startswith("gres/gpu="):
                try:
                    gpu_alloc = int(item.split("=", 1)[1])
                except (ValueError, IndexError):
                    pass
            elif item.startswith("mem="):
                mem_str = item[4:]
                try:
                    if mem_str.endswith("G"):
                        mem_alloc_mb = int(float(mem_str[:-1]) * 1024)
                    elif mem_str.endswith("M"):
                        mem_alloc_mb = int(mem_str[:-1])
                    elif mem_str.endswith("T"):
                        mem_alloc_mb = int(float(mem_str[:-1]) * 1024 * 1024)
                    else:
                        mem_alloc_mb = int(mem_str)
                except (ValueError, IndexError):
                    pass

        stats = NodeStats(
            state=d.get("State", "?").split("+")[0],
            cpu_load=_f("CPULoad"),
            cpu_total=_i("CPUTot"),
            cpu_alloc=_i("CPUAlloc"),
            mem_total_mb=_i("RealMemory"),
            mem_alloc_mb=mem_alloc_mb,
            gpu_total=gpu_total,
            gpu_alloc=gpu_alloc,
        )

        live = _read_gpu_stats_file()
        if live:
            stats.gpu_util        = int(live.get("gpu_util", 0))
            stats.gpu_mem_used_mb = int(live.get("gpu_mem_used_mb", 0))
            stats.gpu_mem_total_mb = int(live.get("gpu_mem_total_mb", 0))
            stats.gpu_temp        = int(live.get("gpu_temp", 0))
            stats.gpu_power_w     = float(live.get("gpu_power_w", 0.0))
            stats.cpu_util        = int(live.get("cpu_util", 0))
            stats.mem_used_mb     = int(live.get("mem_used_mb", 0))
            stats.live_stats      = True

        return stats
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

        # User: inferred from path /…/jobs/{user}/{job_name}/slurm-*.out
        parts = f.parts
        try:
            jobs_idx = next(i for i, p in enumerate(parts) if p == "jobs")
            user = parts[jobs_idx + 1]
        except (StopIteration, IndexError):
            user = "?"

        # Elapsed: birth time via `stat --format=%W` (ext4/btrfs birthtime)
        time_used = _stat_elapsed(f)

        err_file = f.with_suffix(".err")
        try:
            failed = err_file.exists() and err_file.stat().st_size > 0
        except OSError:
            failed = False
        state = "FAILED" if failed else "COMPLETED"

        # Time limit: parse from job.sbatch in same directory
        time_limit = _parse_sbatch_time_limit(f.parent / "job.sbatch")

        result.append(QueueEntry(
            job_id=job_id, name=name, user=user,
            state=state, partition="gpu",
            time_used=time_used, time_limit=time_limit,
            nodes=1,
        ))
    return result


def _stat_elapsed(log_file: Path) -> str:
    """Return elapsed time string by computing mtime - birthtime via stat(1)."""
    try:
        r = subprocess.run(
            ["stat", "--format=%W %Y", str(log_file)],
            capture_output=True, text=True, timeout=3,
        )
        if r.returncode != 0:
            return "-"
        birth_s, mtime_s = r.stdout.split()
        birth, mtime = int(birth_s), int(mtime_s)
        if birth <= 0:
            return "-"   # birthtime not available on this fs
        elapsed = max(0, mtime - birth)
        h, rem = divmod(elapsed, 3600)
        m, s = divmod(rem, 60)
        return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return "-"


def _parse_sbatch_time_limit(sbatch: Path) -> str:
    """Extract --time= value from a job.sbatch file. Returns 'N/A' if absent."""
    try:
        for line in sbatch.read_text(errors="replace").splitlines():
            if line.startswith("#SBATCH") and "--time=" in line:
                return line.split("--time=", 1)[1].strip().split()[0]
    except OSError:
        pass
    return "N/A"


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
