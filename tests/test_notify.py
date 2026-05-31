# tests/test_notify.py
"""Phase 8: notifications (mail directive + poller) and quota surfacing."""
from unittest.mock import patch
import pytest
from iitgpu.jobs import JobSpec, make_job_folder, render_sbatch
from iitgpu import notify
from iitgpu.slurm import QueueEntry


def _spec(**kw):
    base = dict(job_name="j", partition="gpu", gpus=1, cpus=4, mem_gb=8,
                time_limit="01:00:00", run_command="python x.py")
    base.update(kw); return JobSpec(**base)


def test_mail_directives_emitted(tmp_path):
    spec = _spec(mail_user="me@example.edu")
    folder = make_job_folder(str(tmp_path), spec)
    s = render_sbatch(spec, folder)
    assert "#SBATCH --mail-user=me@example.edu" in s
    assert "#SBATCH --mail-type=END,FAIL" in s


def test_no_mail_when_unset(tmp_path):
    spec = _spec()
    folder = make_job_folder(str(tmp_path), spec)
    assert "--mail-user" not in render_sbatch(spec, folder)


def test_mta_present_detection():
    with patch("shutil.which", return_value="/usr/sbin/sendmail"):
        assert notify.mta_present() is True
    with patch("shutil.which", return_value=None):
        assert notify.mta_present() is False


def test_poll_until_done_returns_terminal_state():
    rows = [QueueEntry("55", "j", "COMPLETED", "gpu", "0:30", 1)]
    with patch("iitgpu.notify.slurm.sacct_history", return_value=rows), \
         patch("iitgpu.notify.slurm.queue", return_value=[]):
        state = notify.poll_until_done("55", interval=0)
    assert state == "COMPLETED"


def test_poll_until_done_waits_then_completes():
    seq = [
        [],  # first sacct: not there
        [QueueEntry("55", "j", "FAILED", "gpu", "0:05", 1)],
    ]
    calls = {"n": 0}
    def fake_hist(*a, **k):
        i = min(calls["n"], len(seq) - 1); calls["n"] += 1
        return seq[i]
    with patch("iitgpu.notify.slurm.sacct_history", side_effect=fake_hist), \
         patch("iitgpu.notify.slurm.queue", return_value=[]), \
         patch("time.sleep", return_value=None):
        state = notify.poll_until_done("55", interval=0)
    assert state == "FAILED"
