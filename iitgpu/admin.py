# iitgpu/admin.py
"""Admin panel (Phase 7) — gated to the admin group (config.is_admin()).

Permission gatekeepers applied to every privileged subprocess call:
  • stdin=subprocess.DEVNULL — questionary/prompt_toolkit leaves the PTY in
    raw mode after each prompt; inheriting that as sudo's stdin causes
    "A terminal is required to authenticate" even when NOPASSWD rules match.
    Explicit DEVNULL ensures sudo never tries to read from the terminal.
  • sudo -n (non-interactive) — fails immediately with a clear error if a
    NOPASSWD rule is ever missing, instead of hanging for input.
  • Full absolute paths — avoids PATH-resolution ambiguity in sudo matching.
"""
from __future__ import annotations

import subprocess
from datetime import datetime, timezone, timedelta
from pathlib import Path

from iitgpu import auditclient
from iitgpu.config import load_config, is_admin

# Sri Lanka Standard Time = UTC+5:30
_LK = timezone(timedelta(hours=5, minutes=30))


def _fmt_ts(ts_str: str) -> str:
    """Convert ISO-8601 UTC timestamp to GMT+5:30 display string."""
    try:
        dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        return dt.astimezone(_LK).strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, AttributeError):
        return ts_str[:19]


def _run(cmd: list[str], timeout: int = 15,
         stdin_data: str | None = None) -> tuple[int, str, str]:
    """Run a subprocess with stdin always closed (DEVNULL) unless stdin_data is given.

    This is intentional: questionary puts the PTY in raw mode and doesn't always
    restore it before we call out to sudo. DEVNULL + sudo -n means the call either
    succeeds via NOPASSWD or fails fast — it never hangs waiting for a password.
    """
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

def drain_node(node: str, reason: str) -> tuple[bool, str]:
    if not node or not reason:
        return False, "node and reason are required"
    rc, _, err = _run(["sudo", "-n", "scontrol", "update",
                        f"nodename={node}", "state=drain", f"reason={reason}"])
    auditclient.log("admin_node_drain", detail=f"{node}:{reason}")
    return (rc == 0), ("drained" if rc == 0 else (err.strip() or "drain failed"))


def resume_node(node: str) -> tuple[bool, str]:
    rc, _, err = _run(["sudo", "-n", "scontrol", "update",
                        f"nodename={node}", "state=resume"])
    auditclient.log("admin_node_resume", detail=node)
    return (rc == 0), ("resumed" if rc == 0 else (err.strip() or "resume failed"))


# ── Users ─────────────────────────────────────────────────────────────────────────

def list_gpuusers() -> list[str]:
    """Return members of the gateway group."""
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
    """Set a Linux password non-interactively via chpasswd (login node only)."""
    rc, _, err = _run(
        ["sudo", "-n", "chpasswd"],
        stdin_data=f"{username}:{password}\n",
    )
    return (rc == 0), (err.strip() or "")


def provision_user(username: str, admin: bool = False,
                   password: str = "") -> tuple[bool, str]:
    """Create user on both nodes + SLURM association, then optionally set password."""
    cmd = ["sudo", "-n", "/usr/local/bin/iit-gpu-adduser", username]
    if admin:
        cmd.append("--admin")
    rc, out, err = _run(cmd, timeout=120)
    auditclient.log("admin_provision_user", detail=username)
    if rc != 0:
        return False, err.strip() or "provision failed"
    msg = out.strip()
    if password:
        ok_pw, perr = set_user_password(username, password)
        if ok_pw:
            msg += "\n  ✔  password set"
        else:
            msg += f"\n  ⚠  password not set: {perr or 'chpasswd failed'}"
    return True, msg


def offboard_user(username: str, purge: bool = False) -> tuple[bool, str]:
    cmd = ["sudo", "-n", "/usr/local/bin/iit-gpu-deluser", username]
    if purge:
        cmd.append("--purge-data")
    rc, out, err = _run(cmd, timeout=120)
    auditclient.log("admin_offboard_user", detail=username)
    return (rc == 0), (out.strip() if rc == 0 else (err.strip() or "offboard failed"))


# ── Audit log ─────────────────────────────────────────────────────────────────────

def read_audit(limit: int = 40, action_filter: str = "",
               user_filter: str = "") -> list[dict]:
    """Read recent audit events from JSONL, newest first."""
    import json
    state = Path("/var/lib/iit-gpu/audit.jsonl")
    if not state.exists():
        return []
    try:
        lines = state.read_text(errors="replace").splitlines()
    except OSError:
        return []
    events: list[dict] = []
    for line in reversed(lines):
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if action_filter and action_filter not in ev.get("action", ""):
            continue
        if user_filter and user_filter != ev.get("user", ""):
            continue
        events.append(ev)
        if len(events) >= limit:
            break
    return events


# ── QOS / partitions ──────────────────────────────────────────────────────────────

def list_qos() -> list[dict]:
    """Return QOS entries as structured dicts (name, max_wall, max_gpu, priority)."""
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
    """Set MaxWall for a QOS. Format: HH:MM:SS or D-HH:MM:SS; empty = unlimited."""
    rc, out, err = _run(
        ["sudo", "-n", "sacctmgr", "-i", "modify", "qos", qos_name,
         "set", f"MaxWall={max_wall}"],
        timeout=20,
    )
    auditclient.log("admin_qos_modify", detail=f"{qos_name}:MaxWall={max_wall!r}")
    return (rc == 0), (out.strip() or "updated") if rc == 0 else (err.strip() or "failed")


def set_qos_maxgpu(qos_name: str, max_gpu: int | None) -> tuple[bool, str]:
    """Set MaxTRESPerUser GPU count. Pass None to remove the limit (unlimited)."""
    tres = f"gres/gpu={max_gpu}" if max_gpu is not None else ""
    rc, out, err = _run(
        ["sudo", "-n", "sacctmgr", "-i", "modify", "qos", qos_name,
         "set", f"MaxTRESPerUser={tres}"],
        timeout=20,
    )
    auditclient.log("admin_qos_modify", detail=f"{qos_name}:MaxGPU={max_gpu}")
    return (rc == 0), (out.strip() or "updated") if rc == 0 else (err.strip() or "failed")


def set_qos_priority(qos_name: str, priority: int) -> tuple[bool, str]:
    """Set Priority for a QOS."""
    rc, out, err = _run(
        ["sudo", "-n", "sacctmgr", "-i", "modify", "qos", qos_name,
         "set", f"Priority={priority}"],
        timeout=20,
    )
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
            "Select QOS to edit:",
            choices=qos_names + ["Back"],
            style=style,
        ).ask()
        if qname is None or qname == "Back":
            return

        current = next((r for r in rows if r["name"] == qname), {})

        field = questionary.select(
            "Field to change:",
            choices=["Max Wall Time", "Max GPUs per user", "Priority", "Back"],
            style=style,
        ).ask()
        if field is None or field == "Back":
            continue

        if field == "Max Wall Time":
            info(f"  Current: [magenta]{current.get('max_wall', '?')}[/]")
            info("  Format: HH:MM:SS or D-HH:MM:SS  |  leave blank = unlimited")
            val = questionary.text("New MaxWall:", style=style).ask()
            if val is None:
                continue
            if questionary.confirm(
                    f"Set [magenta]{qname}[/] MaxWall → "
                    f"[magenta]{val.strip() or 'unlimited'}[/]?",
                    default=True, style=style).ask():
                good, msg = set_qos_maxwall(qname, val.strip())
                (ok if good else err)(msg)

        elif field == "Max GPUs per user":
            info(f"  Current: [magenta]{current.get('max_gpu', '?')}[/]")
            val = questionary.text(
                "New max GPUs (positive integer; blank = unlimited):",
                style=style,
            ).ask()
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
                    f"Set [magenta]{qname}[/] Max GPUs → "
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
                    f"Set [magenta]{qname}[/] Priority → [magenta]{prio}[/]?",
                    default=True, style=style).ask():
                good, msg = set_qos_priority(qname, prio)
                (ok if good else err)(msg)


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
            choices=["Drain node", "Resume node", "List users", "Provision user",
                     "Offboard user", "Audit log", "QOS / limits",
                     "Cluster usage (all users)", "Back"],
            style=style,
        ).ask()
        if choice is None or choice == "Back":
            return

        if choice == "Drain node":
            node = questionary.text("Node:", default=node_default, style=style).ask()
            reason = questionary.text("Reason:", style=style).ask()
            good, msg = drain_node(node or node_default, reason or "")
            (ok if good else err)(msg)

        elif choice == "Resume node":
            node = questionary.text("Node:", default=node_default, style=style).ask()
            good, msg = resume_node(node or node_default)
            (ok if good else err)(msg)

        elif choice == "List users":
            for u in list_gpuusers():
                console.print(f"  {u}")

        elif choice == "Provision user":
            u = questionary.text("New username:", style=style).ask()
            if not u:
                questionary.press_any_key_to_continue("").ask()
                continue
            adm = questionary.confirm("Admin?", default=False, style=style).ask()
            pw = questionary.password(
                "Password (leave blank to set later):", style=style,
            ).ask() or ""
            if pw:
                pw2 = questionary.password(
                    "Confirm password:", style=style,
                ).ask() or ""
                if pw != pw2:
                    err("Passwords do not match — user not created.")
                    questionary.press_any_key_to_continue("").ask()
                    continue
            good, msg = provision_user(u.strip(), admin=adm, password=pw)
            (ok if good else err)(msg)
            if good and not pw:
                info(f"[dim]Set a password: sudo passwd {u.strip()}[/]")

        elif choice == "Offboard user":
            u = questionary.text("Username to remove:", style=style).ask()
            if u and questionary.confirm(
                    f"Offboard {u}?", default=False, style=style).ask():
                purge = questionary.confirm(
                    "Purge their /shared data?", default=False, style=style,
                ).ask()
                good, msg = offboard_user(u.strip(), purge=purge)
                (ok if good else err)(msg)

        elif choice == "Audit log":
            af = questionary.text(
                "Filter by action (blank=all):", style=style,
            ).ask() or ""
            uf = questionary.text(
                "Filter by user (blank=all):", style=style,
            ).ask() or ""
            events = read_audit(limit=40, action_filter=af.strip(),
                                user_filter=uf.strip())
            if not events:
                info("No matching events.")
            for ev in events:
                console.print(
                    f"  [dim]{_fmt_ts(ev.get('ts', ''))}[/]  "
                    f"[magenta]{ev.get('user', '?')}[/]  "
                    f"{ev.get('action', '?')}  "
                    f"[dim]{ev.get('detail', '')}[/]"
                )

        elif choice == "QOS / limits":
            _qos_menu(style)

        elif choice == "Cluster usage (all users)":
            from iitgpu.accounting import usage_by_user
            t = Table(show_header=True, header_style="bold cyan")
            for c in ("User", "GPU-h", "CPU-h", "Jobs"):
                t.add_column(c)
            for r in usage_by_user(days=30):
                t.add_row(r.user, f"{r.gpu_hours:.1f}",
                          f"{r.cpu_hours:.1f}", str(r.job_count))
            console.print(t)

        questionary.press_any_key_to_continue("").ask()
