# tests/test_notebooks.py
"""Phase 6: TensorBoard rendering + running services."""
from unittest.mock import patch
import pytest
from iitgpu.jobs import JobSpec, make_job_folder, render_tensorboard_sbatch
from iitgpu.slurm import QueueEntry


def _spec(**kw):
    base = dict(job_name="tensorboard", partition="gpu", gpus=0, cpus=2, mem_gb=8,
                time_limit="08:00:00", run_command="")
    base.update(kw); return JobSpec(**base)


def test_tensorboard_sbatch_has_launch_and_tunnel(tmp_path):
    spec = _spec()
    folder = make_job_folder(str(tmp_path), spec)
    s = render_tensorboard_sbatch(spec, folder, "/shared/u/logs", port=6006,
                                  gateway_host="gw.edu", gateway_port=2225)
    assert "tensorboard --logdir /shared/u/logs" in s
    assert "--host 127.0.0.1" in s
    assert "ssh -p 2225" in s
    assert "6006" in s


def test_tensorboard_binds_localhost_only(tmp_path):
    spec = _spec()
    folder = make_job_folder(str(tmp_path), spec)
    s = render_tensorboard_sbatch(spec, folder, "/shared/logs")
    assert "127.0.0.1" in s
    assert "0.0.0.0" not in s


def test_tensorboard_uses_conda_env(tmp_path):
    spec = _spec(conda_env="/shared/envs/ds")
    folder = make_job_folder(str(tmp_path), spec)
    s = render_tensorboard_sbatch(spec, folder, "/shared/logs")
    assert "conda activate /shared/envs/ds" in s


def test_tensorboard_uses_container(tmp_path):
    spec = _spec(container_image="/shared/images/ds.sif")
    folder = make_job_folder(str(tmp_path), spec)
    s = render_tensorboard_sbatch(spec, folder, "/shared/logs")
    assert "apptainer exec" in s
    assert "conda activate" not in s


def test_running_services_filters_service_jobs(monkeypatch):
    from iitgpu import notebooks
    fake = [
        QueueEntry("1", "notebook", "RUNNING", "gpu", "0:10", 1),
        QueueEntry("2", "train", "RUNNING", "gpu", "0:10", 1),
        QueueEntry("3", "tensorboard", "RUNNING", "gpu", "0:10", 1),
        QueueEntry("4", "interactive", "RUNNING", "gpu", "0:10", 1),
    ]
    with patch("iitgpu.notebooks.queue", return_value=fake):
        svcs = notebooks.running_services()
    names = {s.name for s in svcs}
    assert names == {"notebook", "tensorboard", "interactive"}
    assert all("ssh -p" in s.tunnel for s in svcs)


def test_running_services_empty(monkeypatch):
    from iitgpu import notebooks
    with patch("iitgpu.notebooks.queue", return_value=[]):
        assert notebooks.running_services() == []
