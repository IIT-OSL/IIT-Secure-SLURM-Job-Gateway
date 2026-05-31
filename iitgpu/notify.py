# iitgpu/notify.py
"""Completion notifications (Phase 8): email via SLURM if an MTA exists,
else an in-TUI poller that blocks until the job reaches a terminal state."""
from __future__ import annotations

import shutil
import time

from iitgpu import slurm


def mta_present() -> bool:
    """True if a local mail transfer agent looks available (sendmail/mailx)."""
    return any(shutil.which(b) for b in ("sendmail", "mailx", "mail", "msmtp"))


_TERMINAL = {"COMPLETED", "FAILED", "CANCELLED", "TIMEOUT", "OUT_OF_MEMORY", "NODE_FAIL"}


def poll_until_done(job_id: str, interval: int = 10, max_polls: int = 100000) -> str:
    """Block (polling sacct) until the job is terminal. Returns final state."""
    for _ in range(max_polls):
        rows = slurm.sacct_history(limit=200)
        for r in rows:
            if r.job_id == job_id and r.state in _TERMINAL:
                return r.state
        # also check the live queue — if it's gone from queue and not in history yet
        q = slurm.queue()
        if not any(e.job_id == job_id for e in q):
            rows = slurm.sacct_history(limit=200)
            for r in rows:
                if r.job_id == job_id:
                    return r.state
        time.sleep(interval)
    return "UNKNOWN"
