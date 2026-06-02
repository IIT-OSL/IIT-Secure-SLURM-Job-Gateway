# iitgpu/admin.py
"""Admin panel — gated to the admin group (config.is_admin()).

Permission gatekeepers applied to every privileged subprocess call:
  • stdin=subprocess.DEVNULL — questionary/prompt_toolkit leaves the PTY in
    raw mode after each prompt; inheriting that as sudo stdin causes
    "A terminal is required to authenticate" even when NOPASSWD rules match.
    Explicit DEVNULL ensures sudo never tries to read from the terminal.
  • sudo -n (non-interactive) — fails immediately with a clear error if a
    NOPASSWD rule is ever missing, instead of hanging for input.
  • Full absolute paths — avoids PATH-resolution ambiguity in sudo matching.
"""
from __future__ import annotations

import re
import subprocess
from datetime import datetime, timezone, timedelta
from pathlib import Path

from iitgpu import auditclient
from iitgpu.config import load_config, is_admin
from iitgpu import daemonclient

# Sri Lanka Standard Time = UTC+5:30
_LK = timezone(timedelta(hours=5, minutes=30))

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _fmt_ts(ts_str: str) -> str:
    """Convert ISO-8601 UTC timestamp to GMT+5:30 display string."""
    try:
        dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        return dt.astimezone(_LK).strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, AttributeError):
        return ts_str[:19]


def _run(cmd: list[str], timeout: int = 15,
         stdin_data: str | None = None) -> tuple[int, str, str]:
    """Run a subprocess with stdin always closed (DEVNULL) unless stdin_data is given."""
    try:
        r = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            stdin=subprocess.PIPE if stdin_data is not None else subprocess.DEVNULL,
            input=stdin_data,
        )
        return r.returncode, r.stdout, r.stderr
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, "", str(exc)


# ── Node control ─────────────────────────────────────────────────────────────────

def get_jobs_on_node(node: str) -> list[dict]:
    rc, out, _ = _run(["squeue", "--noheader",
                        "--format=%i|%u|%j|%T", f"--nodelist={node}"])
    if rc != 0 or not out.strip():
        return []
    jobs = []
    for line in out.strip().splitlines():
        parts = line.split("|")
        if len(parts) == 4:
            jobs.append({"id": parts[0].strip(), "user": parts[1].strip(),
                         "name": parts[2].strip(), "state": parts[3].strip()})
    return jobs


def cancel_jobs_on_node(node: str) -> tuple[int, list[str]]:
    jobs = get_jobs_on_node(node)
    cancelled = []
    for j in jobs:
        rc, _, _ = _run(["sudo", "-n", "scancel", j["id"]])
        if rc == 0:
            cancelled.append(j["id"])
            auditclient.log("admin_job_cancel", detail=f"force-drain:{node}",
                            job_id=j["id"])
    return len(cancelled), cancelled


def drain_node(node: str, reason: str,
               cancel_running: bool = False) -> tuple[bool, str]:
    if not node or not reason:
        return False, "node and reason are required"
    cancelled_ids: list[str] = []
    if cancel_running:
        _, cancelled_ids = cancel_jobs_on_node(node)
    rc, _, err = _run(["sudo", "-n", "scontrol", "update",
                        f"nodename={node}", "state=drain", f"reason={reason}"])
    auditclient.log("admin_node_drain", detail=f"{node}:{reason}")
    if rc != 0:
        return False, err.strip() or "drain failed"
    if cancelled_ids:
        return True, f"draining — cancelled {len(cancelled_ids)} job(s): {', '.join(cancelled_ids)}"
    return True, "draining (running jobs will finish before node reaches DRAINED)"


def resume_node(node: str) -> tuple[bool, str]:
    rc, _, err = _run(["sudo", "-n", "scontrol", "update",
                        f"nodename={node}", "state=resume"])
    auditclient.log("admin_node_resume", detail=node)
    return (rc == 0), ("resumed" if rc == 0 else (err.strip() or "resume failed"))


# ── Users ─────────────────────────────────────────────────────────────────────────

def list_gpuusers() -> list[str]:
    cfg = load_config()
    import grp
    import pwd
    try:
        g = grp.getgrnam(cfg.gpuusers_group)
        members = set(g.gr_mem)
        for u in pwd.getpwall():
            if u.pw_gid == g.gr_gid:
                members.add(u.pw_name)
        return sorted(members)
    except KeyError:
        return []


def set_user_password(username: str, password: str) -> tuple[bool, str]:
    rc, _, err = _run(["sudo", "-n", "chpasswd"],
                       stdin_data=f"{username}:{password}\n")
    return (rc == 0), (err.strip() or "")


def provision_user(username: str, admin: bool = False,
                   password: str = "",
                   role: str = "",
                   email: str = "",
                   full_name: str = "",
                   notes: str = "") -> tuple[bool, str]:
    """Create user on both nodes + SLURM association, then write users.db row."""
    cmd = ["sudo", "-n", "/usr/local/bin/iit-gpu-adduser", username]
    if admin or role == "admin":
        cmd.append("--admin")
    elif role == "shell":
        cmd.append("--shell-user")
    rc, out, err = _run(cmd, timeout=120)
    if rc != 0:
        return False, err.strip() or "provision failed"
    msg = out.strip()
    if email:
        effective_role = "admin" if (admin or role == "admin") else role or "tool"
        ok_db, db_msg = daemonclient.create_user(
            username, email, effective_role, full_name, notes)
        if ok_db:
            msg += "\n  ✔  user DB record created"
        else:
            msg += f"\n  ⚠  user DB record failed: {db_msg}"
    auditclient.log("admin_provision_user",
                    detail=username,
                    meta={"role": role or ("admin" if admin else "tool"),
                          "email": email})
    if password:
        ok_pw, perr = set_user_password(username, password)
        msg += "\n  ✔  password set" if ok_pw else f"\n  ⚠  password not set: {perr or 'chpasswd failed'}"
    return True, msg


def offboard_user(username: str, purge: bool = False) -> tuple[bool, str]:
    cmd = ["sudo", "-n", "/usr/local/bin/iit-gpu-deluser", username]
    if purge:
        cmd.append("--purge-data")
    rc, out, err = _run(cmd, timeout=120)
    if rc == 0:
        # Also mark offboarded in users.db (best-effort — script also calls daemoncli)
        daemonclient.offboard_user(username)
        auditclient.log("admin_offboard_user", detail=username)
    return (rc == 0), (out.strip() if rc == 0 else (err.strip() or "offboard failed"))


# ── Audit log ─────────────────────────────────────────────────────────────────────

def read_audit(limit: int = 40, action_filter: str = "",
               user_filter: str = "",
               date_from: str = "", date_to: str = "") -> list[dict]:
    """Read recent audit events via daemon (SQLite), newest first."""
    return daemonclient.query_audit(
        user=user_filter, action=action_filter,
        date_from=date_from, date_to=date_to, limit=limit)


# ── Maintenance notice ────────────────────────────────────────────────────────────

def _maintenance_path() -> str:
    cfg = load_config()
    return f"{cfg.nfs_root}/.maintenance.json"


def get_maintenance() -> dict | None:
    import json
    try:
        data = json.loads(open(_maintenance_path()).read())
        if data.get("active"):
            return data
    except (OSError, ValueError):
        pass
    return None


def set_maintenance(reason: str, set_by: str) -> tuple[bool, str]:
    import json
    import os
    data = {
        "active": True,
        "reason": reason,
        "set_by": set_by,
        "since": datetime.now(timezone.utc).isoformat(),
    }
    try:
        p = _maintenance_path()
        with open(p, "w") as f:
            json.dump(data, f)
        os.chmod(p, 0o666)
        auditclient.log("admin_maintenance_set", detail=reason)
        return True, f"Maintenance notice active: {reason}"
    except OSError as exc:
        return False, str(exc)


def clear_maintenance() -> tuple[bool, str]:
    import os
    try:
        os.remove(_maintenance_path())
    except FileNotFoundError:
        pass
    except OSError as exc:
        return False, str(exc)
    auditclient.log("admin_maintenance_clear", detail="")
    return True, "Maintenance notice cleared."


# ── QOS / partitions ──────────────────────────────────────────────────────────────

def list_qos() -> list[dict]:
    rc, out, _ = _run(["sacctmgr", "-n", "--parsable2", "show", "qos",
                        "format=Name,MaxWall,MaxTRESPerUser,Priority"])
    rows: list[dict] = []
    for line in out.strip().splitlines():
        parts = line.split("|")
        if len(parts) < 4 or not parts[0].strip():
            continue
        name = parts[0].strip()
        max_wall = parts[1].strip() or "unlimited"
        tres = parts[2].strip()
        max_gpu = "unlimited"
        for item in tres.split(","):
            if item.startswith("gres/gpu="):
                max_gpu = item.split("=", 2)[-1]
        rows.append({
            "name": name,
            "max_wall": max_wall,
            "max_gpu": max_gpu,
            "priority": parts[3].strip() or "0",
        })
    return rows


def set_qos_maxwall(qos_name: str, max_wall: str) -> tuple[bool, str]:
    rc, out, err = _run(
        ["sudo", "-n", "sacctmgr", "-i", "modify", "qos", qos_name,
         "set", f"MaxWall={max_wall}"], timeout=20)
    auditclient.log("admin_qos_modify", detail=f"{qos_name}:MaxWall={max_wall!r}")
    return (rc == 0), (out.strip() or "updated") if rc == 0 else (err.strip() or "failed")


def set_qos_maxgpu(qos_name: str, max_gpu: int | None) -> tuple[bool, str]:
    tres = f"gres/gpu={max_gpu}" if max_gpu is not None else ""
    rc, out, err = _run(
        ["sudo", "-n", "sacctmgr", "-i", "modify", "qos", qos_name,
         "set", f"MaxTRESPerUser={tres}"], timeout=20)
    auditclient.log("admin_qos_modify", detail=f"{qos_name}:MaxGPU={max_gpu}")
    return (rc == 0), (out.strip() or "updated") if rc == 0 else (err.strip() or "failed")


def set_qos_priority(qos_name: str, priority: int) -> tuple[bool, str]:
    rc, out, err = _run(
        ["sudo", "-n", "sacctmgr", "-i", "modify", "qos", qos_name,
         "set", f"Priority={priority}"], timeout=20)
    auditclient.log("admin_qos_modify", detail=f"{qos_name}:Priority={priority}")
    return (rc == 0), (out.strip() or "updated") if rc == 0 else (err.strip() or "failed")


# ── QOS sub-menu ──────────────────────────────────────────────────────────────────

def _qos_menu(style) -> None:
    import questionary
    from rich.table import Table
    from iitgpu.ui import console, header, info, ok, err, warn

    while True:
        header("QOS / Limits")
        rows = list_qos()
        if not rows:
            warn("No QOS data (sacctmgr unavailable)."); return

        t = Table(show_header=True, header_style="bold cyan", show_lines=False)
        t.add_column("QOS", style="magenta")
        t.add_column("Max Wall Time")
        t.add_column("Max GPUs / User")
        t.add_column("Priority")
        for r in rows:
            t.add_row(r["name"], r["max_wall"], str(r["max_gpu"]), r["priority"])
        console.print(t)

        qos_names = [r["name"] for r in rows]
        qname = questionary.select(
            "Select QOS to edit:", choices=qos_names + ["Back"], style=style).ask()
        if qname is None or qname == "Back":
            return

        current = next((r for r in rows if r["name"] == qname), {})
        field = questionary.select(
            "Field to change:",
            choices=["Max Wall Time", "Max GPUs per user", "Priority", "Back"],
            style=style).ask()
        if field is None or field == "Back":
            continue

        if field == "Max Wall Time":
            info(f"  Current: [magenta]{current.get('max_wall', '?')}[/]")
            info("  Format: HH:MM:SS or D-HH:MM:SS  |  leave blank = unlimited")
            val = questionary.text("New MaxWall:", style=style).ask()
            if val is None:
                continue
            if questionary.confirm(
                    f"Set [magenta]{qname}[/] MaxWall to "
                    f"[magenta]{val.strip() or 'unlimited'}[/]?",
                    default=True, style=style).ask():
                good, msg = set_qos_maxwall(qname, val.strip())
                (ok if good else err)(msg)

        elif field == "Max GPUs per user":
            info(f"  Current: [magenta]{current.get('max_gpu', '?')}[/]")
            val = questionary.text(
                "New max GPUs (positive integer; blank = unlimited):",
                style=style).ask()
            if val is None:
                continue
            val = val.strip()
            gpu_val: int | None = None
            if val:
                try:
                    gpu_val = int(val)
                    if gpu_val <= 0:
                        raise ValueError
                except ValueError:
                    err("Enter a positive integer or leave blank."); continue
            if questionary.confirm(
                    f"Set [magenta]{qname}[/] Max GPUs to "
                    f"[magenta]{gpu_val if gpu_val is not None else 'unlimited'}[/]?",
                    default=True, style=style).ask():
                good, msg = set_qos_maxgpu(qname, gpu_val)
                (ok if good else err)(msg)

        elif field == "Priority":
            info(f"  Current: [magenta]{current.get('priority', '0')}[/]")
            val = questionary.text("New priority (integer):", style=style).ask()
            if val is None:
                continue
            try:
                prio = int(val.strip())
            except ValueError:
                err("Enter an integer."); continue
            if questionary.confirm(
                    f"Set [magenta]{qname}[/] Priority to [magenta]{prio}[/]?",
                    default=True, style=style).ask():
                good, msg = set_qos_priority(qname, prio)
                (ok if good else err)(msg)


# ── Provision-user sub-flow ───────────────────────────────────────────────────────

def _provision_menu(style) -> None:
    import questionary
    from iitgpu.ui import ok, err, info, warn

    u = questionary.text("New username:", style=style).ask()
    if not u or not u.strip():
        return
    u = u.strip()

    role = questionary.select(
        "User type:",
        choices=[
            questionary.Choice("tool   — forced-TUI, audited (default)", "tool"),
            questionary.Choice("admin  — forced-TUI + admin panel",      "admin"),
            questionary.Choice("shell  — real bash shell, NOT audited",  "shell"),
        ],
        style=style,
    ).ask()
    if role is None:
        return

    if role == "shell":
        warn("[yellow bold]⚠  Shell user warning:[/]")
        warn("[yellow]This grants a real shell on the login node. Their activity is[/]")
        warn("[yellow]NOT audited by the tool. Use only for edge cases the tool[/]")
        warn("[yellow]cannot handle. They are still SLURM-capped via their association.[/]")
        if not questionary.confirm(
                "Understood — proceed with shell user creation?",
                default=False, style=style).ask():
            return

    full_name = questionary.text("Full name:", style=style).ask() or ""
    email = questionary.text("Email address:", style=style).ask() or ""
    if email and not _EMAIL_RE.match(email.strip()):
        err("Invalid email address — user DB record will be skipped.")
        email = ""
    email = email.strip()
    notes = questionary.text("Notes (optional):", style=style).ask() or ""
    pw = questionary.password(
        "Password (leave blank to set later):", style=style).ask() or ""
    if pw:
        pw2 = questionary.password("Confirm password:", style=style).ask() or ""
        if pw != pw2:
            err("Passwords do not match — user not created.")
            return

    good, msg = provision_user(
        u, admin=(role == "admin"), role=role, password=pw,
        email=email, full_name=full_name.strip(), notes=notes.strip())
    (ok if good else err)(msg)
    if good and not pw:
        info(f"[dim]Set a password: sudo passwd {u}[/]")


# ── Admin log viewers ─────────────────────────────────────────────────────────────

def _view_audit_log(style) -> None:
    import questionary
    from iitgpu.ui import console, header, info

    header("Audit Log")
    uf  = questionary.text("Filter by user (blank=all):", style=style).ask() or ""
    af  = questionary.text("Filter by action (blank=all):", style=style).ask() or ""
    df  = questionary.text("Date from (YYYY-MM-DD, blank=any):", style=style).ask() or ""
    dt  = questionary.text("Date to   (YYYY-MM-DD, blank=any):", style=style).ask() or ""
    lim = questionary.text("Limit (default 40):", style=style).ask() or "40"
    try:
        limit = int(lim)
    except ValueError:
        limit = 40
    date_from = (df.strip() + "T00:00:00+00:00") if df.strip() else ""
    date_to   = (dt.strip() + "T23:59:59+00:00") if dt.strip() else ""
    events = read_audit(limit=limit, action_filter=af.strip(),
                        user_filter=uf.strip(),
                        date_from=date_from, date_to=date_to)
    if not events:
        info("No matching events.")
        return
    for ev in events:
        meta_str = ""
        if ev.get("meta"):
            meta_str = f"  [dim]{ev['meta']}[/]"
        console.print(
            f"  [dim]{_fmt_ts(ev.get('ts', ''))}[/]  "
            f"[magenta]{ev.get('user', '?')}[/]  "
            f"{ev.get('action', '?')}  "
            f"[dim]{ev.get('detail', '')}[/]"
            f"{meta_str}"
        )


def _view_users(style) -> None:
    import questionary
    from rich.table import Table
    from iitgpu.ui import console, header, info, warn

    header("User Roster")
    data = daemonclient.view_roster()
    users = data.get("users", [])
    drift = data.get("drift", {})
    db_only = set(drift.get("db_only", []))
    os_only = set(drift.get("os_only", []))

    if not users and not os_only:
        info("No users in database (daemon may be unavailable)."); return

    t = Table(show_header=True, header_style="bold cyan", show_lines=False)
    t.add_column("Username",   style="magenta")
    t.add_column("Full name")
    t.add_column("Email")
    t.add_column("Role")
    t.add_column("Status")
    t.add_column("Created at")
    t.add_column("Created by")
    t.add_column("Flags",      style="yellow")
    for u in users:
        flags = []
        if u["username"] in db_only:
            flags.append("DB-only")
        t.add_row(
            u["username"], u.get("full_name", ""), u.get("email", ""),
            u["role"], u["status"], _fmt_ts(u.get("created_at", "")),
            u.get("created_by", ""), ", ".join(flags))
    console.print(t)

    if os_only:
        warn(f"[yellow]OS-only (in group but no DB row):[/] {', '.join(sorted(os_only))}")
    if db_only:
        warn(f"[yellow]DB-only (DB row but no OS group):[/] {', '.join(sorted(db_only))}")

    questionary.press_any_key_to_continue("").ask()


def _view_maillog(style) -> None:
    import questionary
    from iitgpu.ui import console, header, info

    header("Mail Delivery Log  (/var/log/msmtp.log)")
    lines = daemonclient.tail_maillog(lines=60)
    if not lines:
        info("Log empty or unavailable (check daemon + /var/log/msmtp.log).")
    else:
        for line in lines:
            console.print(f"  [dim]{line}[/]")
    questionary.press_any_key_to_continue("").ask()


def _view_job_output(style) -> None:
    import questionary
    from iitgpu.ui import console, header, info, err

    header("User Job Output")
    target_user = questionary.text("Username:", style=style).ask()
    if not target_user or not target_user.strip():
        return
    target_user = target_user.strip()
    cfg = load_config()
    jobs_base = Path(cfg.nfs_root) / cfg.jobs_subdir / target_user
    if not jobs_base.exists():
        info(f"No job directory for {target_user}")
        return
    # Collect .out/.err files
    files = sorted(
        f.name for jd in jobs_base.iterdir() if jd.is_dir()
        for f in jd.iterdir()
        if f.suffix in (".out", ".err")
    ) if jobs_base.exists() else []
    if not files:
        info(f"No .out/.err files for {target_user}")
        return
    fname = questionary.select("Select file:", choices=files + ["Back"],
                               style=style).ask()
    if fname is None or fname == "Back":
        return
    # Find the full relative path
    rel = None
    for jd in jobs_base.iterdir():
        if jd.is_dir() and (jd / fname).exists():
            rel = f"{jd.name}/{fname}"
            break
    if rel is None:
        err("File not found.")
        return
    good, content = daemonclient.read_job_log(target_user, rel)
    if not good:
        err(f"Cannot read: {content}")
        return
    header(f"Job output: {target_user}/{rel}")
    console.print(content[:8000])   # first 8k chars
    questionary.press_any_key_to_continue("").ask()


def _view_service_health(style) -> None:
    import questionary
    from iitgpu.ui import console, header, info

    _UNITS = ["iit-gpu-audit", "slurmctld", "slurmd", "mariadb", "slurmdbd"]
    header("Service Health")
    unit = questionary.select("Select service:", choices=_UNITS + ["Back"],
                              style=style).ask()
    if unit is None or unit == "Back":
        return
    good, data = daemonclient.service_status(unit)
    if not good:
        from iitgpu.ui import err
        err(f"Cannot get status: {data.get('error', '?')}"); return
    active = data.get("active", "unknown")
    color = "green" if active == "active" else "red"
    console.print(f"\n  [{color}]● {unit}[/]  status: [{color}]{active}[/]\n")
    journal = data.get("journal", "")
    if journal:
        info("[dim]Recent journal entries:[/]")
        for line in journal.splitlines()[-20:]:
            console.print(f"  [dim]{line}[/]")
    questionary.press_any_key_to_continue("").ask()


# ── Main admin menu ───────────────────────────────────────────────────────────────

def admin_menu() -> None:
    import questionary
    from questionary import Style
    from rich.table import Table
    from iitgpu.ui import console, header, info, ok, err, warn

    cfg = load_config()
    if not is_admin(cfg):
        warn("Admin panel is restricted to members of the admin group.")
        return

    style = Style([("qmark", "fg:cyan bold"), ("pointer", "fg:cyan bold")])
    node_default = "iit-MS-7E06"

    while True:
        header("Admin Panel")
        choice = questionary.select(
            "Admin action:",
            choices=[
                "Drain node",
                "Resume node",
                "Provision user",
                "Offboard user",
                "View users",
                "Audit log",
                "All-user job history",
                "Cluster usage (all users)",
                "Any user's job output",
                "Mail delivery log",
                "Service health",
                "QOS / limits",
                "Set maintenance notice",
                "Clear maintenance notice",
                "Back",
            ],
            style=style,
        ).ask()
        if choice is None or choice == "Back":
            return

        if choice == "Drain node":
            node = questionary.text("Node:", default=node_default, style=style).ask()
            node = (node or node_default).strip()
            reason = questionary.text("Reason:", style=style).ask()
            if not reason or not reason.strip():
                err("A drain reason is required.")
                questionary.press_any_key_to_continue("").ask()
                continue
            running = get_jobs_on_node(node)
            cancel_running = False
            if running:
                info(f"  [yellow]{len(running)} job(s) currently on {node}:[/]")
                for j in running:
                    info(f"    job {j['id']}  user={j['user']}  "
                         f"name={j['name']}  [{j['state']}]")
                cancel_running = questionary.confirm(
                    "Cancel these jobs now? (force drain)",
                    default=False, style=style).ask()
            else:
                info(f"  [dim]No jobs running on {node}.[/]")
            good, msg = drain_node(node, reason.strip(), cancel_running=cancel_running)
            (ok if good else err)(msg)

        elif choice == "Resume node":
            node = questionary.text("Node:", default=node_default, style=style).ask()
            good, msg = resume_node(node or node_default)
            (ok if good else err)(msg)

        elif choice == "Provision user":
            _provision_menu(style)

        elif choice == "Offboard user":
            u = questionary.text("Username to remove:", style=style).ask()
            if u and questionary.confirm(
                    f"Offboard {u}?", default=False, style=style).ask():
                purge = questionary.confirm(
                    "Purge their /shared data?", default=False, style=style).ask()
                good, msg = offboard_user(u.strip(), purge=purge)
                (ok if good else err)(msg)

        elif choice == "View users":
            _view_users(style)
            continue

        elif choice == "Audit log":
            _view_audit_log(style)

        elif choice == "All-user job history":
            from iitgpu.monitor import filtered_history
            from iitgpu.slurm import filtered_history as _fh
            t = Table(show_header=True, header_style="bold cyan")
            for c in ("Job ID", "User", "Name", "State", "Elapsed", "Partition"):
                t.add_column(c)
            for entry in (_fh(all_users=True) if hasattr(_fh, "__call__") else []):
                t.add_row(entry.job_id, entry.user, entry.name, entry.state,
                          entry.elapsed, entry.partition)
            console.print(t)

        elif choice == "Cluster usage (all users)":
            from iitgpu.accounting import usage_by_user
            t = Table(show_header=True, header_style="bold cyan")
            for c in ("User", "GPU-h", "CPU-h", "Jobs"):
                t.add_column(c)
            for r in usage_by_user(days=30):
                t.add_row(r.user, f"{r.gpu_hours:.1f}",
                          f"{r.cpu_hours:.1f}", str(r.job_count))
            console.print(t)

        elif choice == "Any user's job output":
            _view_job_output(style)
            continue

        elif choice == "Mail delivery log":
            _view_maillog(style)
            continue

        elif choice == "Service health":
            _view_service_health(style)
            continue

        elif choice == "QOS / limits":
            _qos_menu(style)

        elif choice == "Set maintenance notice":
            current = get_maintenance()
            if current:
                info(f"  [yellow]Active notice:[/] {current.get('reason', '')}")
            reason = questionary.text(
                "Maintenance reason (shown to all users on login):",
                style=style).ask()
            if reason and reason.strip():
                import os
                good, msg = set_maintenance(
                    reason.strip(), set_by=os.environ.get("USER", "admin"))
                (ok if good else err)(msg)

        elif choice == "Clear maintenance notice":
            current = get_maintenance()
            if not current:
                info("No active maintenance notice.")
            elif questionary.confirm(
                    "Clear the maintenance notice?",
                    default=True, style=style).ask():
                good, msg = clear_maintenance()
                (ok if good else err)(msg)

        questionary.press_any_key_to_continue("").ask()
