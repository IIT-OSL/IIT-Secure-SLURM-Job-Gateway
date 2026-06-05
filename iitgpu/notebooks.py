# iitgpu/notebooks.py
"""Running interactive services (Phase 6): notebooks + TensorBoard.

Lists active notebook/TensorBoard/interactive jobs, shows the SSH tunnel
command for each, and offers one-key teardown (scancel).
"""
from __future__ import annotations

from dataclasses import dataclass

from iitgpu.slurm import queue, cancel
from iitgpu.config import load_config

_SERVICE_NAMES = {"notebook", "tensorboard", "interactive"}


@dataclass
class Service:
    job_id: str
    name: str
    state: str
    tunnel: str


def running_services() -> list[Service]:
    """Return active notebook/tensorboard/interactive jobs with tunnel hints."""
    cfg = load_config()
    out: list[Service] = []
    for e in queue():
        if e.name in _SERVICE_NAMES:
            # Exact port lives in the job's stdout; show the generic tunnel shape.
            tunnel = (f"ssh -p {cfg.gateway_port} -L <port>:localhost:<port> "
                      f"<you>@{cfg.gateway_host}   (see job {e.job_id} output for the port)")
            out.append(Service(e.job_id, e.name, e.state, tunnel))
    return out


def services_menu() -> None:
    import questionary
    from questionary import Style
    from iitgpu.ui import header, info, ok, err, console

    style = Style([("qmark", "fg:cyan bold"), ("pointer", "fg:cyan bold")])
    while True:
        header("My Running Services")
        svcs = running_services()
        if not svcs:
            info("No active notebooks / TensorBoard / interactive sessions.")
            return
        for s in svcs:
            console.print(f"  [magenta]{s.job_id}[/]  {s.name}  [{s.state}]")
            console.print(f"      [dim]{s.tunnel}[/]")
        choices = [f"Stop {s.job_id} ({s.name})" for s in svcs] + ["Refresh", "Back"]
        choice = questionary.select("Action:", choices=choices, style=style).ask()
        if choice is None or choice == "Back":
            return
        if choice == "Refresh":
            continue
        jid = choice.split()[1]
        if questionary.confirm(f"Stop service job {jid}?", default=False, style=style).ask():
            good, msg = cancel(jid)
            (ok if good else err)(str(msg))


def launch_tensorboard() -> None:
    """Submit a TensorBoard service job pointed at a chosen logdir."""
    import getpass
    from pathlib import Path
    import questionary
    from questionary import Style
    from iitgpu.config import jobs_dir
    from iitgpu.jobs import JobSpec, make_job_folder, render_tensorboard_sbatch, resource_defaults
    from iitgpu.slurm import submit_job
    from iitgpu.ui import header, info, ok, err, kv, panel
    from iitgpu.validate import in_jail
    from iitgpu import auditclient

    style = Style([("qmark", "fg:cyan bold"), ("pointer", "fg:cyan bold")])
    cfg = load_config()
    header("Launch TensorBoard")
    logdir = questionary.text("Log directory to visualise:",
                              default=str(Path(cfg.nfs_root) / getpass.getuser()),
                              style=style).ask()
    if not logdir or not in_jail(logdir.strip()):
        err("Invalid or out-of-jail log directory."); return
    port = questionary.text("Port:", default="6006", style=style).ask()
    try:
        port = max(1024, min(65535, int(port.strip())))
    except (ValueError, AttributeError):
        port = 6006

    d = resource_defaults("inference")
    spec = JobSpec(job_name="tensorboard", partition=cfg.partition, gpus=0,
                   cpus=2, mem_gb=8, time_limit="08:00:00", run_command="",
                   task_type="tensorboard")
    # Auto-populate SLURM mail directive from users.db if an MTA is available.
    from iitgpu.notify import mta_present
    from iitgpu import daemonclient
    if mta_present():
        _tb_email = daemonclient.email_for(getpass.getuser())
        if _tb_email:
            spec.mail_user = _tb_email
    folder = make_job_folder(jobs_dir(cfg), spec)
    script = render_tensorboard_sbatch(spec, folder, logdir.strip(), port=port,
                                       gateway_host=cfg.gateway_host,
                                       gateway_port=int(cfg.gateway_port))
    panel("TensorBoard sbatch", script)
    sb = str(Path(folder) / "job.sbatch")
    Path(sb).write_text(script); Path(sb).chmod(0o644)
    if not auditclient.log_or_block("tensorboard_submit", detail=logdir):
        err("Audit logging failed."); return
    good, res = submit_job(sb)
    if good:
        ok(f"TensorBoard submitted! Job {res}")
        ok(f"Tunnel: ssh -p {cfg.gateway_port} -L {port}:localhost:{port} "
           f"{getpass.getuser()}@{cfg.gateway_host}")
    else:
        err(f"Submission failed: {res}")
