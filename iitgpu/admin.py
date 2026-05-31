# iitgpu/admin.py
"""Admin panel (Phase 7) — gated to the admin group (config.is_admin()).

Node drain/resume, QOS/partition view, user list + provision/offboard,
audit-log viewer, and cluster-wide usage. Privileged actions run via sudo,
which the post-cutover sudoers limits to %<admin_group>.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from iitgpu import auditclient
from iitgpu.config import load_config, is_admin


def _run(cmd: list[str], timeout: int = 15) -> tuple[int, str, str]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout, r.stderr
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, "", str(exc)


# ── Node control ────────────────────────────────────────────────────────────────

def drain_node(node: str, reason: str) -> tuple[bool, str]:
    if not node or not reason:
        return False, "node and reason are required"
    rc, _, err = _run(["sudo", "scontrol", "update",
                       f"nodename={node}", "state=drain", f"reason={reason}"])
    auditclient.log("admin_node_drain", detail=f"{node}:{reason}")
    return (rc == 0), ("drained" if rc == 0 else (err.strip() or "drain failed"))


def resume_node(node: str) -> tuple[bool, str]:
    rc, _, err = _run(["sudo", "scontrol", "update", f"nodename={node}", "state=resume"])
    auditclient.log("admin_node_resume", detail=node)
    return (rc == 0), ("resumed" if rc == 0 else (err.strip() or "resume failed"))


# ── Users ────────────────────────────────────────────────────────────────────────

def list_gpuusers() -> list[str]:
    """Return members of the gateway group."""
    cfg = load_config()
    import grp
    try:
        g = grp.getgrnam(cfg.gpuusers_group)
        members = set(g.gr_mem)
        # include users whose primary gid is this group
        import pwd
        for u in pwd.getpwall():
            if u.pw_gid == g.gr_gid:
                members.add(u.pw_name)
        return sorted(members)
    except KeyError:
        return []


def provision_user(username: str, admin: bool = False) -> tuple[bool, str]:
    cmd = ["sudo", "iit-gpu-adduser", username]
    if admin:
        cmd.append("--admin")
    rc, out, err = _run(cmd, timeout=120)
    auditclient.log("admin_provision_user", detail=username)
    return (rc == 0), (out.strip() if rc == 0 else (err.strip() or "provision failed"))


def offboard_user(username: str, purge: bool = False) -> tuple[bool, str]:
    cmd = ["sudo", "iit-gpu-deluser", username]
    if purge:
        cmd.append("--purge-data")
    rc, out, err = _run(cmd, timeout=120)
    auditclient.log("admin_offboard_user", detail=username)
    return (rc == 0), (out.strip() if rc == 0 else (err.strip() or "offboard failed"))


# ── Audit log ────────────────────────────────────────────────────────────────────

def read_audit(limit: int = 40, action_filter: str = "", user_filter: str = "") -> list[dict]:
    """Read recent audit events from the JSONL state file, newest first."""
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


# ── QOS / partitions ─────────────────────────────────────────────────────────────

def qos_table() -> str:
    rc, out, _ = _run(["sacctmgr", "-n", "show", "qos",
                       "format=Name,MaxWall,MaxTRESPerUser,Priority"])
    return out.strip() or "no QOS data"


# ── TUI ───────────────────────────────────────────────────────────────────────────

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
            if u:
                adm = questionary.confirm("Admin?", default=False, style=style).ask()
                good, msg = provision_user(u.strip(), admin=adm)
                (ok if good else err)(msg)
        elif choice == "Offboard user":
            u = questionary.text("Username to remove:", style=style).ask()
            if u and questionary.confirm(f"Offboard {u}?", default=False, style=style).ask():
                purge = questionary.confirm("Purge their /shared data?", default=False, style=style).ask()
                good, msg = offboard_user(u.strip(), purge=purge)
                (ok if good else err)(msg)
        elif choice == "Audit log":
            af = questionary.text("Filter by action (blank=all):", style=style).ask() or ""
            for ev in read_audit(limit=40, action_filter=af.strip()):
                console.print(f"  [dim]{ev.get('ts','')[:19]}[/]  "
                              f"[magenta]{ev.get('user','?')}[/]  {ev.get('action','?')}  "
                              f"[dim]{ev.get('detail','')}[/]")
        elif choice == "QOS / limits":
            console.print(qos_table())
        elif choice == "Cluster usage (all users)":
            from iitgpu.accounting import usage_by_user
            t = Table(show_header=True, header_style="bold cyan")
            for c in ("User", "GPU-h", "CPU-h", "Jobs"):
                t.add_column(c)
            for r in usage_by_user(days=30):
                t.add_row(r.user, f"{r.gpu_hours:.1f}", f"{r.cpu_hours:.1f}", str(r.job_count))
            console.print(t)
        questionary.press_any_key_to_continue("").ask()
