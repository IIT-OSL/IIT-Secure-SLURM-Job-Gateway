# tests/test_e2e.py
import json
import os
import socket
import sys
import subprocess
from pathlib import Path
import pytest


def test_selftest_passes():
    result = subprocess.run(
        [sys.executable, "-m", "iitgpu", "--selftest"],
        capture_output=True, text=True,
        env={**os.environ, "DEMO_MODE": "1"},
    )
    assert result.returncode == 0, f"selftest failed:\n{result.stdout}\n{result.stderr}"
    assert "All checks passed" in result.stdout


def test_demo_submit_and_queue(tmp_path, monkeypatch):
    monkeypatch.setenv("DEMO_MODE", "1")
    monkeypatch.setenv("NFS_ROOT", str(tmp_path))

    from iitgpu.config import load_config, jobs_dir
    from iitgpu.jobs import JobSpec, make_job_folder, write_sbatch
    from iitgpu.slurm import queue, submit_job

    cfg = load_config()
    jdir = jobs_dir(cfg)

    spec = JobSpec(
        job_name="e2e_test",
        partition="gpu-short",
        gpus=1,
        cpus=4,
        mem_gb=8,
        time_limit="00:30:00",
        run_command="echo hello",
        modules=["CUDA/11.8"],
    )
    folder = make_job_folder(jdir, spec)
    sbatch_path = write_sbatch(spec, folder)

    assert Path(sbatch_path).exists()
    script = Path(sbatch_path).read_text()
    assert f"#SBATCH --job-name={Path(folder).name}" in script
    assert "#SBATCH --gres=gpu:1" in script

    success, job_id = submit_job(sbatch_path)
    assert success is True
    assert job_id.isdigit()

    jobs = queue()
    assert any(e.job_id == job_id for e in jobs)


@pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="Unix sockets not available")
def test_three_audit_events_spooled(tmp_path, monkeypatch):
    spool_dir = tmp_path / "spool"
    monkeypatch.setenv("AUDIT_SOCKET", str(tmp_path / "no_daemon.sock"))
    monkeypatch.setenv("AUDIT_SPOOL", str(spool_dir))

    import importlib
    import iitgpu.auditclient as ac
    importlib.reload(ac)

    ac.log("e2e_event_1", detail="test")
    ac.log("e2e_event_2", detail="test")
    ac.log("e2e_event_3", detail="test")

    spooled = list(spool_dir.iterdir()) if spool_dir.exists() else []
    assert len(spooled) == 3
    actions = {json.loads(f.read_text())["action"] for f in spooled}
    assert actions == {"e2e_event_1", "e2e_event_2", "e2e_event_3"}
