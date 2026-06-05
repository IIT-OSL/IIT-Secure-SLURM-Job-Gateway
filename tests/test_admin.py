# tests/test_admin.py
"""Phase 7: admin panel — gating, node control, users, audit, QOS."""
from unittest.mock import patch, MagicMock, call
import json
import pytest
from iitgpu import admin


def _proc(rc=0, out="", err=""):
    m = MagicMock(); m.returncode = rc; m.stdout = out; m.stderr = err
    return m


# ── Gate ──────────────────────────────────────────────────────────────────────

def test_admin_menu_blocked_for_non_admin(capsys):
    with patch("iitgpu.admin.is_admin", return_value=False):
        admin.admin_menu()


# ── Timestamp formatting ──────────────────────────────────────────────────────

def test_fmt_ts_converts_utc_to_lk():
    # 2026-06-01T00:00:00+00:00 UTC  →  2026-06-01 05:30:00 GMT+5:30
    result = admin._fmt_ts("2026-06-01T00:00:00+00:00")
    assert result == "2026-06-01 05:30:00"


def test_fmt_ts_handles_z_suffix():
    result = admin._fmt_ts("2026-06-01T00:00:00Z")
    assert result == "2026-06-01 05:30:00"


def test_fmt_ts_handles_bad_input():
    result = admin._fmt_ts("not-a-timestamp")
    assert result == "not-a-timestamp"  # shorter than 19 chars, returned as-is


def test_fmt_ts_handles_empty():
    result = admin._fmt_ts("")
    assert result == ""


# ── Node control ──────────────────────────────────────────────────────────────

def test_drain_node_uses_sudo_n():
    with patch("subprocess.run", return_value=_proc()) as r:
        ok, msg = admin.drain_node("node1", "maintenance")
    # drain_node calls squeue (get_jobs_on_node) then scontrol
    scontrol_call = next(c for c in r.call_args_list
                         if "scontrol" in c[0][0])
    cmd = scontrol_call[0][0]
    assert cmd[:3] == ["sudo", "-n", "scontrol"]
    assert "nodename=node1" in cmd
    assert "state=drain" in cmd
    assert "reason=maintenance" in cmd
    assert ok


def test_drain_node_requires_reason():
    ok, _ = admin.drain_node("node1", "")
    assert not ok


def test_drain_node_force_cancels_jobs():
    squeue_out = "42|public|train|RUNNING\n"
    responses = [_proc(out=squeue_out), _proc(), _proc()]  # squeue, scancel, scontrol
    with patch("subprocess.run", side_effect=responses) as r:
        ok, msg = admin.drain_node("node1", "maint", cancel_running=True)
    assert ok
    assert "42" in msg
    scancel_call = r.call_args_list[1][0][0]
    assert "scancel" in scancel_call
    assert "42" in scancel_call


def test_get_jobs_on_node_parses_squeue():
    out = "42|public|train|RUNNING\n99|daham|test|RUNNING\n"
    with patch("subprocess.run", return_value=_proc(out=out)):
        jobs = admin.get_jobs_on_node("iit-MS-7E06")
    assert len(jobs) == 2
    assert jobs[0]["id"] == "42" and jobs[0]["user"] == "public"
    assert jobs[1]["id"] == "99"


def test_get_jobs_on_node_empty():
    with patch("subprocess.run", return_value=_proc(out="")):
        jobs = admin.get_jobs_on_node("iit-MS-7E06")
    assert jobs == []


def test_resume_node_uses_sudo_n():
    with patch("subprocess.run", return_value=_proc()) as r:
        ok, _ = admin.resume_node("node1")
    cmd = r.call_args[0][0]
    assert cmd[:3] == ["sudo", "-n", "scontrol"]
    assert "state=resume" in cmd
    assert ok


# ── Users ─────────────────────────────────────────────────────────────────────

def test_provision_user_uses_full_path_and_sudo_n():
    with patch("subprocess.run", return_value=_proc(out="done")) as r:
        ok, _ = admin.provision_user("alice", admin=True)
    cmd = r.call_args_list[0][0][0]
    assert cmd[0] == "sudo"
    assert cmd[1] == "-n"
    assert cmd[2] == "/usr/local/bin/iit-gpu-adduser"
    assert "alice" in cmd
    assert "--admin" in cmd
    assert ok


def test_provision_user_sets_password_via_chpasswd():
    with patch("subprocess.run", return_value=_proc(out="done")) as r, \
         patch("iitgpu.admin.daemonclient.create_user", return_value=(True, "ok")), \
         patch("iitgpu.admin.auditclient.log"):
        ok, msg = admin.provision_user("alice", password="s3cr3t", email="a@b.com")
    assert ok
    cmds = [c[0][0] for c in r.call_args_list]
    assert any("iit-gpu-adduser" in " ".join(c) for c in cmds)
    chpasswd_call = next(c for c in r.call_args_list if "chpasswd" in c[0][0])
    assert "alice:s3cr3t\n" in (chpasswd_call[1].get("input") or "")


def test_provision_user_welcome_sent_with_password():
    """send_welcome must receive the initial password so it can be emailed to the user."""
    with patch("subprocess.run", return_value=_proc(out="done")), \
         patch("iitgpu.admin.daemonclient.create_user", return_value=(True, "ok")), \
         patch("iitgpu.admin.auditclient.log"), \
         patch("iitgpu.mailer.send_welcome", return_value=(True, "sent")) as mock_welcome:
        admin.provision_user("alice", password="s3cr3t",
                             email="alice@iit.lk", full_name="Alice")
        import time; time.sleep(0.05)
    assert mock_welcome.called
    args, kwargs = mock_welcome.call_args
    assert "s3cr3t" in str(args) or "s3cr3t" in str(kwargs), \
        "send_welcome must receive the initial password"


def test_provision_user_must_change_pw_flag_set_when_password_given():
    """create_user must be called with must_change_pw=True when a password is set."""
    with patch("subprocess.run", return_value=_proc(out="done")), \
         patch("iitgpu.admin.daemonclient.create_user", return_value=(True, "ok")) as mock_cu, \
         patch("iitgpu.admin.auditclient.log"):
        admin.provision_user("alice", password="s3cr3t",
                             email="alice@iit.lk", role="tool")
    assert mock_cu.called
    _, kwargs = mock_cu.call_args
    assert kwargs.get("must_change_pw") is True


def test_provision_user_must_change_pw_false_when_no_password():
    """must_change_pw must be False when no password is provided at provision time."""
    with patch("subprocess.run", return_value=_proc(out="done")), \
         patch("iitgpu.admin.daemonclient.create_user", return_value=(True, "ok")) as mock_cu, \
         patch("iitgpu.admin.auditclient.log"):
        admin.provision_user("alice", password="", email="alice@iit.lk", role="tool")
    assert mock_cu.called
    _, kwargs = mock_cu.call_args
    assert kwargs.get("must_change_pw") is False


def test_provision_user_skips_password_on_adduser_failure():
    with patch("subprocess.run", return_value=_proc(rc=1, err="adduser failed")) as r:
        ok, msg = admin.provision_user("alice", password="s3cr3t")
    assert not ok
    assert r.call_count == 1  # chpasswd never called


def test_set_user_password_pipes_to_chpasswd():
    with patch("subprocess.run", return_value=_proc()) as r:
        ok, _ = admin.set_user_password("bob", "pass123")
    assert ok
    cmd = r.call_args[0][0]
    assert cmd == ["sudo", "-n", "chpasswd"]
    assert r.call_args[1].get("input") == "bob:pass123\n"


def test_offboard_user_uses_full_path_and_sudo_n():
    with patch("subprocess.run", return_value=_proc(out="done")) as r:
        ok, _ = admin.offboard_user("bob", purge=True)
    cmd = r.call_args[0][0]
    assert cmd[0] == "sudo"
    assert cmd[1] == "-n"
    assert cmd[2] == "/usr/local/bin/iit-gpu-deluser"
    assert "--purge-data" in cmd
    assert ok


def test_run_always_uses_devnull_stdin():
    """_run passes stdin=DEVNULL unless stdin_data is given."""
    import subprocess as sp
    with patch("subprocess.run", return_value=_proc()) as r:
        admin._run(["echo", "hi"])
    kwargs = r.call_args[1]
    assert kwargs["stdin"] == sp.DEVNULL


def test_run_uses_pipe_when_stdin_data_given():
    import subprocess as sp
    with patch("subprocess.run", return_value=_proc()) as r:
        admin._run(["cat"], stdin_data="hello\n")
    kwargs = r.call_args[1]
    assert kwargs["input"] == "hello\n"
    assert "stdin" not in kwargs


# ── Audit log ─────────────────────────────────────────────────────────────────

def test_read_audit_filters_by_action():
    evs_data = [{"ts": "2026-05-31T10:00:00+00:00", "user": "alice",
                 "action": "job_submit"}]
    with patch("iitgpu.admin.daemonclient.query_audit", return_value=evs_data):
        evs = admin.read_audit(action_filter="job_submit")
    assert len(evs) == 1 and evs[0]["user"] == "alice"


def test_read_audit_filters_by_user():
    evs_data = [{"ts": "2026-05-31T10:01:00+00:00", "user": "bob",
                 "action": "job_cancel"}]
    with patch("iitgpu.admin.daemonclient.query_audit", return_value=evs_data):
        evs = admin.read_audit(user_filter="bob")
    assert len(evs) == 1 and evs[0]["action"] == "job_cancel"


# ── QOS ───────────────────────────────────────────────────────────────────────

_QOS_OUTPUT = "normal|08:00:00|gres/gpu=1|0\nlong|7-00:00:00||0\n"


def test_list_qos_parses_sacctmgr_output():
    with patch("subprocess.run", return_value=_proc(out=_QOS_OUTPUT)):
        rows = admin.list_qos()
    assert len(rows) == 2
    normal = rows[0]
    assert normal["name"] == "normal"
    assert normal["max_wall"] == "08:00:00"
    assert normal["max_gpu"] == "1"
    assert normal["priority"] == "0"
    long_qos = rows[1]
    assert long_qos["max_wall"] == "7-00:00:00"
    assert long_qos["max_gpu"] == "unlimited"


def test_set_qos_maxwall_calls_sacctmgr():
    with patch("subprocess.run", return_value=_proc(out="Modified")) as r:
        ok, _ = admin.set_qos_maxwall("normal", "12:00:00")
    cmd = r.call_args[0][0]
    assert cmd[:3] == ["sudo", "-n", "sacctmgr"]
    assert "modify" in cmd and "qos" in cmd and "normal" in cmd
    assert "MaxWall=12:00:00" in cmd
    assert ok


def test_set_qos_maxwall_empty_clears_limit():
    with patch("subprocess.run", return_value=_proc(out="Modified")) as r:
        ok, _ = admin.set_qos_maxwall("normal", "")
    cmd = r.call_args[0][0]
    assert "MaxWall=" in cmd
    assert ok


def test_set_qos_maxgpu_sets_tres():
    with patch("subprocess.run", return_value=_proc(out="Modified")) as r:
        ok, _ = admin.set_qos_maxgpu("normal", 2)
    cmd = r.call_args[0][0]
    assert "MaxTRESPerUser=gres/gpu=2" in cmd
    assert ok


def test_set_qos_maxgpu_none_clears_limit():
    with patch("subprocess.run", return_value=_proc(out="Modified")) as r:
        ok, _ = admin.set_qos_maxgpu("long", None)
    cmd = r.call_args[0][0]
    assert "MaxTRESPerUser=" in cmd
    assert ok


def test_set_qos_priority():
    with patch("subprocess.run", return_value=_proc(out="Modified")) as r:
        ok, _ = admin.set_qos_priority("normal", 100)
    cmd = r.call_args[0][0]
    assert "Priority=100" in cmd
    assert ok


# ── All-user job history ──────────────────────────────────────────────────────

def test_filtered_history_accepts_all_users_flag():
    """filtered_history must accept (search_root, all_users=True) without TypeError."""
    from iitgpu.slurm import filtered_history, QueueEntry
    fake = [QueueEntry("10", "alice", "COMPLETED", "gpu", "1:00", 1)]
    with patch("iitgpu.slurm._sacct_history_user", return_value=fake):
        rows = filtered_history("/shared/jobs", all_users=True, days=30)
    assert any(r.job_id == "10" for r in rows)


# ── list_gpuusers ─────────────────────────────────────────────────────────────

def test_list_gpuusers_returns_sorted():
    fake_grp = MagicMock(gr_mem=["bob", "alice"], gr_gid=1500)
    with patch("grp.getgrnam", return_value=fake_grp), \
         patch("pwd.getpwall", return_value=[]):
        users = admin.list_gpuusers()
    assert users == ["alice", "bob"]


# ── Disk usage ────────────────────────────────────────────────────────────────

def test_disk_usage_by_user_sums_per_user(tmp_path):
    alice = tmp_path / "alice" / "job1"
    alice.mkdir(parents=True)
    (alice / "out.log").write_bytes(b"x" * 1024)
    (alice / "err.log").write_bytes(b"y" * 512)

    bob = tmp_path / "bob" / "job1"
    bob.mkdir(parents=True)
    (bob / "out.log").write_bytes(b"z" * 2048)

    rows = admin.disk_usage_by_user(str(tmp_path))
    by_user = {r["user"]: r for r in rows}

    assert by_user["alice"]["bytes"] == 1536
    assert by_user["bob"]["bytes"] == 2048


def test_disk_usage_by_user_sorted_descending(tmp_path):
    for user, size in [("alice", 100), ("charlie", 5000), ("bob", 300)]:
        d = tmp_path / user / "j"
        d.mkdir(parents=True)
        (d / "f").write_bytes(b"x" * size)

    rows = admin.disk_usage_by_user(str(tmp_path))
    assert rows[0]["user"] == "charlie"
    assert rows[-1]["user"] == "alice"


def test_disk_usage_by_user_empty_dir(tmp_path):
    assert admin.disk_usage_by_user(str(tmp_path)) == []


def test_disk_usage_by_user_nonexistent_root(tmp_path):
    assert admin.disk_usage_by_user(str(tmp_path / "no_such_dir")) == []


def test_disk_usage_human_readable_units(tmp_path):
    d = tmp_path / "alice" / "j"
    d.mkdir(parents=True)
    (d / "f").write_bytes(b"x" * (2 * 1024 * 1024))  # 2 MB

    rows = admin.disk_usage_by_user(str(tmp_path))
    assert rows[0]["human"] == "2.0 MB"

# ── M4: username validation ───────────────────────────────────────────────────

def test_valid_username_accepts_normal():
    assert admin.valid_username("alice")
    assert admin.valid_username("bob_2")
    assert admin.valid_username("user-name")
    assert admin.valid_username("_svc")


def test_valid_username_rejects_dangerous():
    assert not admin.valid_username("../etc")
    assert not admin.valid_username("a/b")
    assert not admin.valid_username("has space")
    assert not admin.valid_username("Has.Dot")
    assert not admin.valid_username("UPPER")
    assert not admin.valid_username("")
    assert not admin.valid_username("9startsdigit")
    assert not admin.valid_username("x" * 40)


def test_provision_user_rejects_bad_username():
    with patch("subprocess.run") as r:
        ok, msg = admin.provision_user("../../etc/passwd", password="x", email="a@b.com")
    assert not ok
    assert "invalid username" in msg.lower()
    r.assert_not_called()   # never reaches sudo adduser


def test_offboard_user_rejects_bad_username():
    with patch("subprocess.run") as r:
        ok, msg = admin.offboard_user("../../etc")
    assert not ok
    r.assert_not_called()
