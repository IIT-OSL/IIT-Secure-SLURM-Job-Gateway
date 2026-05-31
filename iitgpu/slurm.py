from __future__ import annotations
import json
import os
import subprocess
import time
from dataclasses import dataclass
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
    cpu_load5: float = 0.0
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



# ── Gateway identity helpers ───────────────────────────────────────────────────
# When GATEWAY_SHARED_USER is on (legacy), SLURM CLI runs as the shared account
# (e.g. "daham") via sudo. When off (default / per-user identity), commands run
# directly as the logged-in user and attribute correctly in sacct/fairshare.

def _gateway_prefix() -> list[str]:
    from iitgpu.config import load_config
    cfg = load_config()
    if cfg.gateway_shared_user:
        return ["sudo", "-u", cfg.shared_user]
    return []


def _effective_user() -> str:
    import getpass
    from iitgpu.config import load_config
    cfg = load_config()
    return cfg.shared_user if cfg.gateway_shared_user else getpass.getuser()

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
            _gateway_prefix() + ["sbatch", script_path],
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
    # them all.  %u only returns the SLURM owner ("daham"), and %j only returns
    # the short --job-name ("train").  The output path (%o) encodes both the
    # real gateway user and the full job-folder name, so we parse it instead.
    # Use | separator so paths with spaces (rare but possible) don't break parsing.
    _eu = _effective_user()
    cmd = _gateway_prefix() + ["squeue", "--noheader",
           "--format=%i|%j|%u|%T|%P|%M|%l|%D|%o", "-u", _eu]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        entries = []
        for line in result.stdout.splitlines():
            parts = line.split("|")
            if len(parts) < 8:
                continue
            name = parts[1]
            user_val = parts[2]
            out_path = parts[8].strip() if len(parts) > 8 else ""
            if out_path:
                path_parts = Path(out_path).parts
                try:
                    jobs_idx = next(i for i, s in enumerate(path_parts) if s == "jobs")
                    user_val = path_parts[jobs_idx + 1]
                    name = path_parts[jobs_idx + 2]
                except (StopIteration, IndexError):
                    pass
            entries.append(QueueEntry(
                job_id=parts[0], name=name, user=user_val,
                state=parts[3], partition=parts[4],
                time_used=parts[5], time_limit=parts[6],
                nodes=int(parts[7]) if parts[7].isdigit() else 1,
            ))
        return entries
    except (OSError, subprocess.TimeoutExpired):
        return []


def _read_gpu_stats_file() -> dict | None:
    """Return live GPU/CPU/RAM stats.

    Primary source: /shared/.gpu_stats.json written every 2 s by the
    iit-gpu-stats-writer daemon on the compute node.
    Fallback: call nvidia-smi + /proc directly when the file is stale or
    missing (e.g. after a reboot before the daemon restarts).
    """
    try:
        p = Path(_GPU_STATS_FILE)
        if p.exists():
            age = time.time() - p.stat().st_mtime
            if age <= _GPU_STATS_MAX_AGE:
                return json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        pass

    # ── Direct fallback ────────────────────────────────────────────────────────
    return _read_hw_stats_direct()


def _read_hw_stats_direct() -> dict | None:
    """Query nvidia-smi and /proc directly — used when the stats daemon is down."""
    stats: dict = {}
    try:
        r = subprocess.run(
            ["nvidia-smi",
             "--query-gpu=utilization.gpu,utilization.memory,"
             "memory.used,memory.total,temperature.gpu,power.draw",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode == 0:
            parts = [p.strip() for p in r.stdout.strip().split(",")]
            stats["gpu_util"]         = int(parts[0])
            stats["gpu_mem_util"]     = int(parts[1])
            stats["gpu_mem_used_mb"]  = int(parts[2])
            stats["gpu_mem_total_mb"] = int(parts[3])
            stats["gpu_temp"]         = int(parts[4])
            stats["gpu_power_w"]      = float(parts[5])
    except (OSError, subprocess.TimeoutExpired, ValueError, IndexError):
        pass

    try:
        vals = Path("/proc/loadavg").read_text().split()
        import os as _os
        ncpu = _os.cpu_count() or 1
        stats["cpu_load1"] = float(vals[0])
        stats["cpu_load5"] = float(vals[1])
        stats["cpu_util"]  = min(int(float(vals[0]) / ncpu * 100), 100)
    except (OSError, ValueError, IndexError):
        pass

    try:
        mem: dict[str, int] = {}
        for line in Path("/proc/meminfo").read_text().splitlines():
            k, v = line.split(":", 1)
            mem[k.strip()] = int(v.strip().split()[0])
        total = mem.get("MemTotal", 0) // 1024
        avail = mem.get("MemAvailable", 0) // 1024
        stats["mem_total_mb"] = total
        stats["mem_used_mb"]  = total - avail
    except (OSError, ValueError):
        pass

    if not stats:
        return None
    stats["ts"] = time.time()
    return stats


def _count_running_gpu_jobs() -> int:
    """Return number of currently RUNNING jobs that requested a GPU.
    Used because AllocTRES omits GPU on this SLURM build."""
    try:
        r = subprocess.run(
            _gateway_prefix() + ["squeue", "--noheader",
             "--states=RUNNING", "--format=%b"],   # %b = requested GRES
            capture_output=True, text=True, timeout=5,
        )
        return sum(1 for line in r.stdout.splitlines() if "gpu" in line.lower())
    except (OSError, subprocess.TimeoutExpired):
        return 0


def get_node_stats(node_name: str = "iit-MS-7E06") -> NodeStats | None:
    """Return live stats: SLURM allocation from scontrol + actual utilization from stats file."""
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

        # AllocTRES doesn't include GPU on this SLURM build — count running GPU
        # jobs from squeue instead, which is authoritative.
        gpu_alloc    = _count_running_gpu_jobs()
        mem_alloc_mb = 0
        for item in d.get("AllocTRES", "").split(","):
            if item.startswith("mem="):
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
            stats.cpu_load5       = float(live.get("cpu_load5", 0.0))
            stats.mem_used_mb     = int(live.get("mem_used_mb", 0))
            stats.live_stats      = True

        return stats
    except (OSError, subprocess.TimeoutExpired):
        return None


# == SACCT-based history (requires slurmdbd) ================================

# Job states that count as "history" (terminal). Running/pending jobs are shown
# by queue(), not here. We filter in Python instead of via sacct --state because
# sacct's --state filter without matching end-time semantics silently drops
# already-completed jobs (returns nothing), making the dashboard history empty.
_SACCT_TERMINAL_STATES = {
    "COMPLETED", "FAILED", "CANCELLED", "TIMEOUT",
    "OUT_OF_MEMORY", "NODE_FAIL", "PREEMPTED", "BOOT_FAIL", "DEADLINE",
}


def sacct_history(limit: int = 20, user: str | None = None, days: int = 30) -> list[QueueEntry]:
    """Return completed-job history via sacct (newest-first).

    Uses an explicit start window (-S now-<days>days). The sacct --state CLI
    filter is intentionally NOT used (it drops completed jobs when no -S end
    semantics match); terminal states are filtered in Python instead.
    """
    if user is None:
        user = _effective_user()
    try:
        result = subprocess.run(
            _gateway_prefix() + [
                "sacct",
                "--noheader",
                "--parsable2",
                "-X",
                "--format=JobID,JobName,User,State,Elapsed,Start,End,AllocTRES",
                "--user", user,
                "-S", f"now-{days}days",
            ],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode != 0:
            return []
        entries: list[QueueEntry] = []
        for line in result.stdout.splitlines():
            parts = line.split("|")
            if len(parts) < 8:
                continue
            job_id = parts[0]
            if "." in job_id:
                continue
            state = parts[3].split()[0] if parts[3].split() else parts[3]
            if state not in _SACCT_TERMINAL_STATES:
                continue   # skip RUNNING/PENDING/SUSPENDED — those live in queue()
            entries.append(QueueEntry(
                job_id=job_id,
                name=parts[1] or job_id,
                user=parts[2] or user,
                state=state,
                partition="gpu",
                time_used=parts[4] or "-",
                time_limit="N/A",
                nodes=1,
            ))
        return list(reversed(entries))[:limit]
    except (OSError, subprocess.TimeoutExpired):
        return []


def job_history(search_root: str, limit: int = 20) -> list[QueueEntry]:
    """Return job history: sacct when slurmdbd is up, file-scan otherwise."""
    from iitgpu.config import load_config
    cfg = load_config()
    if cfg.sacct_enabled:
        rows = sacct_history(limit=limit)
        if rows:
            return rows
    return recent_jobs(search_root, limit=min(limit, 20))


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
            _gateway_prefix() + ["scancel", job_id],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            return True, f"Job {job_id} cancelled"
        return False, result.stderr.strip()
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
