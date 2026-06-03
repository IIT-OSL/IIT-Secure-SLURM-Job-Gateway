# M05 — IIT Secure SLURM Job Gateway: Mail Infrastructure, User Management & Admin UX

**Date:** 2026-06-03
**Author:** Daham Dissanayake
**Status:** Deployed on `main` (commit `d4d223a`) · live at `/opt/iit-gpu/` on the login node (192.168.122.10)
**Tests:** 510 passing — `PYTHONPATH=/opt/iit-gpu python3 -m pytest tests/ -q`
**Repo:** `https://github.com/DahamDissanayake/IIT-Secure-SLURM-Job-Gateway`
**Builds on:** M04 (definitive architecture reference)

This is a clean, consolidated milestone document. It supersedes the earlier
session-by-session draft and describes the system **as it is deployed today**,
including the latest change — the **admin mail-privacy fix** that stops admins
being BCC'd on user-facing mail and gives them their own dedicated
"new user created" notice instead.

---

## Table of Contents

1. [What This Milestone Delivers](#1-what-this-milestone-delivers)
2. [Admin Mail Privacy — The Latest Fix](#2-admin-mail-privacy--the-latest-fix)
3. [Mail Architecture — Full Picture](#3-mail-architecture--full-picture)
4. [Transactional Mail Service — iitgpu/mailer.py](#4-transactional-mail-service--iitgpumailerpy)
5. [User Management — Lifecycle Hardening](#5-user-management--lifecycle-hardening)
6. [Bug Fixes](#6-bug-fixes)
7. [Timezone Hardening — GMT+5:30 Everywhere](#7-timezone-hardening--gmt530-everywhere)
8. [Admin Panel Redesign](#8-admin-panel-redesign)
9. [System Fix — gpusync adm Group](#9-system-fix--gpusync-adm-group)
10. [Architecture Delta](#10-architecture-delta)
11. [Daemon Verb Reference](#11-daemon-verb-reference)
12. [Commits](#12-commits)
13. [Active State](#13-active-state)

---

## 1. What This Milestone Delivers

| Area | Outcome |
|------|---------|
| Transactional mail | `iitgpu/mailer.py` — welcome, login, offboard, and admin "new user created" notice |
| Mail privacy | User-facing mail goes **only to the user**; admins are never silently BCC'd |
| Admin notification | Dedicated "new user created" email sent **directly to each admin**, no credentials |
| Daemon mail relay | `mail.send` verb holds the Resend API key; clients never see it (C1) |
| Anti-relay | Non-admin callers can only mail their own registered address |
| Login dedup | New-IP login notices deduplicated server-side in the daemon (M3) |
| User lifecycle | Welcome on create, login notice per session, offboard notice on deactivate |
| Re-provisioning | Offboarded usernames can be re-created (upsert, no UNIQUE crash) |
| Timezone | All user-visible timestamps/filenames use GMT+5:30 |
| Admin panel | Grouped 4-section menu with separators; consolidated maintenance entry |
| System | `gpusync` added to `adm` so the daemon can read `/var/log/msmtp.log` |
| Resilience | Resend API requests carry a `User-Agent` (avoids Cloudflare 1010 block) |

---

## 2. Admin Mail Privacy — The Latest Fix

### Symptom

When a new account (`dahamedge`) was created, **all** admins (`dahamadmin`,
`indrajith`) received copies of the account's mail — not just a heads-up, but the
user-facing welcome/credential flow itself.

### Root cause

The audit daemon's `mail.send` handler (`_h_mail_send`) auto-added **every active
admin as BCC to every email sent by an admin/root caller** whenever no explicit
BCC was supplied:

```python
# deploy/audit_daemon.py — BEFORE
else:
    if not to:
        return False, None, "recipient required"
    if not bcc:
        rows = users_conn.execute(
            "SELECT email FROM users WHERE role='admin' AND status='active' "
            "AND email != ''").fetchall()
        bcc = [r[0] for r in rows if r[0] != to]
```

Because account provisioning runs as an admin/root caller, the welcome email — and
login notices, and offboard notices — were all silently copied to every admin. The
mailer functions even passed `bcc=None` *intending* privacy, but the daemon
overrode that intent.

### Fix

Two coordinated changes:

**1. Daemon stops auto-BCCing admins** (`deploy/audit_daemon.py`):

```python
# AFTER
else:
    if not to:
        return False, None, "recipient required"
    # No auto-BCC: admins are notified via dedicated emails, never by silently
    # copying them on user-facing mail. Only an explicit bcc (if any) is sent.

ok_send, msg = _resend_send(to, subject, html, bcc or None)
```

User-facing mail (welcome, login, offboard) now goes **only to its recipient**.
An explicitly supplied BCC is still honoured, so the relay is not crippled.

**2. Admins get their own dedicated notice** (`iitgpu/mailer.py` +
`iitgpu/admin.py`). A new email is sent **directly to each admin** — only when a
user is created, carrying **no password and no credentials**:

```python
# iitgpu/mailer.py
def send_user_created_admin_notice(admin_email, username, email,
                                   full_name="", role="", created_by=""):
    """Admins' OWN email — sent to each admin directly, fired only on user
    creation. No credentials: just who was created, by whom, and when."""
    ...
    return _send_sync(admin_email, subject, html, bcc=None, kind="admin_notice")
```

```python
# iitgpu/admin.py — provision_user(), after the user's welcome email
try:
    from iitgpu import mailer as _mailer
    created_by  = getpass.getuser()
    admin_addrs = [a for a in daemonclient.admin_emails() if a]
    for _addr in admin_addrs:
        _mailer.send_user_created_admin_notice(
            _addr, username, email, full_name, notice_role, created_by)
    # ... audit log "admin_notice_sent"
except Exception as exc:          # best-effort: never block provisioning
    auditclient.log("mail_failed", detail=f"admin_notice:{username}", ...)
```

### Result

| Email | Before | After |
|-------|--------|-------|
| Welcome / credentials | To user **+ BCC all admins** | **To user only** |
| Login notification | To user **+ BCC all admins** | **To user only** |
| Offboard notice | To user **+ BCC all admins** | **To user only** |
| New user created | *(did not exist)* | **To each admin directly — no credentials, on creation only** |

The "new user created" notice contains: name, username, email, role, created-by,
and timestamp. The initial password is never emailed — it is still handed to the
user in person, exactly as the welcome email instructs.

> **Scope note:** SLURM job-lifecycle emails (`iit-gpu-mailer`, Path A in §3)
> still BCC admins — that path builds its own recipient list and does not go
> through `mail.send`. That behaviour is left intentionally for ops visibility;
> if admins should also be removed from job-event BCC, that is a follow-up.

Commit: `d4d223a`

---

## 3. Mail Architecture — Full Picture

### Two parallel mail paths

**Path A — SLURM job lifecycle (`iit-gpu-mailer`)**

```
Job reaches terminal state
  → slurmctld reads MailProg=/usr/local/bin/iit-gpu-mailer from slurm.conf
  → iit-gpu-mailer -s "SLURM Job_id=N Name=X Ended ..." user@email
      ├── parse subject → extract job_id, event type
      ├── sacct -j <job_id> → live details
      ├── query /run/iit-gpu/audit.sock → users.admin_emails → BCC list
      ├── build HTML
      └── curl POST api.resend.com/emails  (to: user, bcc: admins)
          fallback: /usr/bin/msmtp (plain text, no BCC)
```

Events: `STARTED` · `ENDED` · `FAILED` · `TIMEOUT` · `REQUEUED` · `OOM`

**Path B — User lifecycle (`iitgpu/mailer.py` → daemon `mail.send`)**

```
Account created  →  provision_user()
                      ├─ Thread → send_welcome()            → user only
                      └─ send_user_created_admin_notice()   → each admin (no creds)

User logs in     →  __main__.main()
                      └─ send_login_notification()          → user only (new-IP dedup)

Account removed  →  offboard_user()
                      └─ send_offboard()                    → user only
```

The Resend API key lives **only on the daemon** (`secrets.env`, `0640 root:gpusync`).
No user or admin process reads it; clients hand the built message to the daemon's
`mail.send` verb, which enforces the trust model below.

### `mail.send` trust model (daemon `_h_mail_send`)

| Caller | Recipient | BCC | Notes |
|--------|-----------|-----|-------|
| admin / root | any `to` | only an **explicit** bcc | **No auto-BCC of admins** |
| non-admin | forced to caller's own registered address | stripped | anti-relay (C1/M2) |
| `kind="login"` | self | — | server-side new-IP dedup (M3) |

### Complete email matrix

| Email | Trigger | To | BCC | Accent |
|-------|---------|-----|-----|--------|
| Welcome / credentials | Account created | New user | — | Blue |
| **New user created** | Account created | **Each admin** | — | **Violet** |
| Login detected | Every TUI `session_start` (new IP) | User | — | Green |
| Account deactivated | Offboard | User | — | Grey |
| Job started | SLURM BEGIN | Job owner | All admins | Blue |
| Job completed | SLURM END (COMPLETED) | Job owner | All admins | Green |
| Job failed | SLURM FAIL | Job owner | All admins | Red |
| Job timed out | SLURM TIMEOUT | Job owner | All admins | Amber |
| Job OOM | SLURM OOM | Job owner | All admins | Red |
| Job requeued | SLURM REQUEUE | Job owner | All admins | Purple |

All emails share the visual template: 4 px accent top bar · dark header `#111827` ·
white content `#FFFFFF` · monospace detail table · footer with LK timestamp and
"By: IIT Research Team".

---

## 4. Transactional Mail Service — iitgpu/mailer.py

### Design principles

- **Key never leaves the daemon:** `mailer.py` contains no API key and does no
  HTTP. Every send routes through `_daemon_mail` → `mail.send` (C1).
- **Privacy by default:** user-facing mail is addressed only to the user. Admins
  are informed through their own dedicated emails, never by silent BCC.
- **Non-blocking where safe:** login notices fire on a background thread; welcome
  and offboard are synchronous so their success/failure is reported to the admin.
- **Fail-closed for credentials:** if the welcome email fails, the admin is told to
  hand credentials in person; provisioning is never silently assumed to be mailed.
- **GMT+5:30 timestamps:** all displayed times use the `_LK` timezone constant.

### Public API

```python
send_welcome(username, email, full_name="")                       -> (ok, msg)
send_login_notification(username, email, remote_ip)               -> None  (fire-and-forget)
send_offboard(username, email, full_name="")                      -> (ok, msg)
send_user_created_admin_notice(admin_email, username, email,
                               full_name="", role="", created_by="") -> (ok, msg)
```

> Note: `send_welcome` has **no password parameter** — the initial password is
> never emailed. The welcome mail tells the user the admin will provide it in person.

### Internal helpers

```python
_daemon_mail(to, subject, html, bcc=None, kind="generic", ip="")  # → daemon mail.send
_send_sync(to, subject, html, bcc=None, kind="generic")           # must-deliver, returns (ok,msg)
_fire(to, subject, html, bcc=None, kind="generic", ip="")         # background thread
```

### Email kinds

| `kind` | Email | Recipient handling on daemon |
|--------|-------|------------------------------|
| `welcome` | Account ready | admin caller → to user, no BCC |
| `admin_notice` | New user created | admin caller → to one admin, no BCC |
| `login` | Login detected | forced to self; new-IP dedup |
| `offboard` | Account deactivated | admin caller → to user, no BCC |

---

## 5. User Management — Lifecycle Hardening

### Full user lifecycle

```
Admin → "Provision user"
  → _provision_menu(): username, role, full name, email, notes, password
  → provision_user()
      ├── iit-gpu-adduser  (OS user both nodes, SLURM assoc, /shared/users/<u>/)
      ├── daemonclient.create_user()  → daemon _h_users_create() [upsert if offboarded]
      ├── auditclient.log("admin_provision_user")
      ├── set_user_password() via chpasswd
      ├── Thread → mailer.send_welcome()                  → user only
      └── mailer.send_user_created_admin_notice() × admins → each admin (no creds)

User logs in via SSH (ForceCommand: iit-gpu-manager)
  → __main__.main()
      ├── auditclient.log("session_start")
      └── mailer.send_login_notification()                → user only (new-IP only)

Admin → "Offboard user"
  → daemonclient.get_user()  [fetch record BEFORE deletion]
  → offboard_user()
      ├── iit-gpu-deluser (removes OS user, SLURM assoc, optionally purges /shared)
      ├── daemonclient.offboard_user() → UPDATE status='offboarded'
      ├── auditclient.log("admin_offboard_user")
      └── mailer.send_offboard()                          → user only
```

### Re-provisioning an offboarded username (upsert)

Offboarding sets `status='offboarded'` — it does not delete the row, so a plain
`INSERT` on re-creation hit the `username` UNIQUE constraint. `_h_users_create`
now checks first:

| Scenario | Result |
|----------|--------|
| New username | `INSERT` |
| Offboarded username | `UPDATE` — row restored to `active` with new details |
| Active username | Rejected: `user 'X' already exists and is active` |

The previous tenure's audit trail is preserved independently in `audit_events`.

### Users DB schema

```sql
CREATE TABLE users (
    username   TEXT PRIMARY KEY,
    uid        INTEGER,
    full_name  TEXT NOT NULL DEFAULT '',
    email      TEXT NOT NULL DEFAULT '',
    role       TEXT NOT NULL CHECK (role IN ('admin','tool','shell')),
    status     TEXT NOT NULL CHECK (status IN ('active','offboarded')),
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL,
    notes      TEXT NOT NULL DEFAULT '',
    must_change_pw INTEGER NOT NULL DEFAULT 0
);
```

---

## 6. Bug Fixes

### 6.1 Admin BCC leak on user-facing mail
See §2. Daemon no longer auto-BCCs admins; dedicated admin notice added. (`d4d223a`)

### 6.2 All-user job history crash — `entry.elapsed`
`admin.py` referenced `entry.elapsed`; `QueueEntry` only ever had `time_used`.
Renamed the reference. (`70f225f`)

### 6.3 Mail delivery log panel always empty
`/var/log/msmtp.log` is `root:adm 640`; the daemon (`gpusync`) was not in `adm`, so
reads raised `PermissionError` (caught as empty). Fixed by adding `gpusync` to `adm`
(see §9).

### 6.4 Re-provisioning an offboarded username — UNIQUE constraint
`_h_users_create` now upserts (see §5). (`c4204ad`)

### 6.5 Resend blocked by Cloudflare (1010)
Resend API requests now send a `User-Agent: iit-gpu-mailer/1.0` header in both
`deploy/audit_daemon.py` (`_resend_send`) and `deploy/iit-gpu-mailer`
(`send_resend`). (`ada8872`)

---

## 7. Timezone Hardening — GMT+5:30 Everywhere

The login node's clock is `Etc/UTC`, so naive `datetime.now()` returns UTC. Two
places stamped user-visible paths with naive `now()`, making folder names look
5 h 30 m in the past.

| Usage | Pattern | Reason |
|-------|---------|--------|
| Storage / DB timestamps | `datetime.now(timezone.utc).isoformat()` | Unambiguous; convertible |
| Displayed timestamps | `dt.astimezone(_LK).strftime(...)` | GMT+5:30 for Sri Lanka |
| User-visible filenames | `datetime.now(_LK).strftime(...)` | Direct LK timestamp |

Fixed: job folder names (`iitgpu/jobs.py`) and inline-paste data files
(`iitgpu/wizard.py`) now use `_LK = timezone(timedelta(hours=5, minutes=30))`.
Audit timestamps were already stored as UTC ISO-8601 and converted on display via
`_fmt_ts()`. (`a498570`)

---

## 8. Admin Panel Redesign

Flat 15-item list → 4 grouped sections with `questionary.Separator` dividers:

```
──  User Management  ──   Provision user · Offboard user · View users
──  Jobs & Usage  ────   All-user job history · Cluster usage · Disk usage · Any user's job output
──  Cluster Control  ──   Drain node · Resume node · QOS / limits · Maintenance notice
──  Monitoring  ──────   Audit log · Service health · Mail delivery log
```

Choices carry a leading two-space indent for hierarchy; the result is `.strip()`'d
before dispatch. The two maintenance items were merged into one smart
`_maintenance_menu()` (set when none active; update/clear when active). Added a
missing `press_any_key_to_continue` after the cluster-usage view. (`0e12765`)

---

## 9. System Fix — gpusync adm Group

`/var/log/msmtp.log` is `root:adm 640`. The audit daemon runs as `gpusync`, which
was not in `adm`, so the mail-log viewer always reported "Log empty".

```bash
sudo usermod -aG adm gpusync
sudo systemctl restart iit-gpu-audit
# gpusync groups after: gpusync gpuusers auditadmin adm
```

`adm` is the standard Debian/Ubuntu log-reader group, so this also covers future
`/var/log/` files created with the same ownership. **This is infrastructure state,
not in git** — include it in any cluster re-provisioning runbook.

---

## 10. Architecture Delta

### Module map (changed files marked ←)

```
iitgpu/
├── __main__.py       ← login notification on session_start
├── admin.py          ← grouped menu, _maintenance_menu, welcome+offboard mail,
│                        new "user created" admin notice, getpass import
├── daemonclient.py   ← admin_emails()
├── jobs.py           ← GMT+5:30 _LK constant
├── mailer.py         ← welcome / login / offboard + send_user_created_admin_notice
└── wizard.py         ← GMT+5:30 _LK constant

deploy/
├── audit_daemon.py   ← mail.send (no admin auto-BCC), users.admin_emails,
│                        upsert in _h_users_create, User-Agent header
└── iit-gpu-mailer    ← daemon admin_emails BCC (job path), User-Agent header
```

### Mail-flow delta (this milestone)

```
BEFORE: every admin/root-sent email → daemon auto-BCC all admins
        → welcome/login/offboard silently copied to every admin

AFTER:  user-facing mail → recipient only
        new user created → dedicated email to each admin (no credentials)
        SLURM job events → still BCC admins (separate path, unchanged)
```

---

## 11. Daemon Verb Reference

| Verb | Auth | Description |
|------|------|-------------|
| `audit.log` | Any | Record an audit event |
| `mail.send` | Any (role-gated inside) | Send transactional mail; key held by daemon; no admin auto-BCC |
| `users.email_for` | Any (self or admin) | Get email for one user |
| `users.admin_emails` | **Any** | Get all active admin emails (used by job-mail BCC) |
| `users.create` | Admin | Create or re-activate user record (upsert) |
| `users.get` | Admin | Get one user record |
| `users.list` | Admin | List all users |
| `users.offboard` | Admin | Mark user as offboarded |
| `users.reconcile` | Admin | Find OS/DB mismatches |
| `users.check_must_change_pw` | Self or admin | First-login password-change gate |
| `audit.query` | Admin | Query audit event log |
| `roster.view` | Admin | View combined user roster |
| `maillog.tail` | Admin | Read `/var/log/msmtp.log` |
| `joblog.read` | Admin | Read a user's job output file |
| `service.status` | Admin | Check systemd unit health |

---

## 12. Commits

| Hash | Message |
|------|---------|
| `70f225f` | `fix(admin): use time_used instead of nonexistent elapsed on QueueEntry` |
| `a498570` | `fix(tz): use GMT+5:30 for all user-visible timestamps (job folders, data files)` |
| `646e277` | `feat(mail): welcome email on user creation, login notifications` |
| `c4204ad` | `fix(users): re-provision offboarded username by updating existing row instead of INSERT` |
| `0e12765` | `feat(admin): grouped admin panel with sections, offboard email notification` |
| `1a97623` | `security: daemon-held API key (C1), sbatch jail (H1/M1), admin_emails gate (M2), login dedup (M3), username validation (M4), fail-closed mail-user (L1)` |
| `c027661` | `fix(provision): repair shell-user provisioning and site.env sourcing` |
| `ada8872` | `fix(mail): set User-Agent on Resend API requests (Cloudflare 1010 block)` |
| `d4d223a` | `fix(mail): stop admin BCC leak; send dedicated new-user notice` |

---

## 13. Active State

### Services

| Service | Node | State |
|---------|------|-------|
| `slurmctld` | login (192.168.122.10) | active |
| `slurmd` | GPU host | active |
| `slurmdbd` | login (192.168.122.10) | active |
| `mariadb` | login (192.168.122.10) | active |
| `iit-gpu-audit` | login (192.168.122.10) | active (restarted 15:20:58 UTC, 2026-06-03) |
| `iit-gpu-stats` | GPU host | active |

### Tests

```
510 passed   (PYTHONPATH=/opt/iit-gpu python3 -m pytest tests/ -q)
```

### Mail delivery

| Path | From | Recipients | Status |
|------|------|-----------|--------|
| SLURM job events | iit-gpu-mailer → Resend API | job owner + BCC admins | Live |
| Welcome (on create) | iitgpu/mailer → daemon → Resend | **user only** | Live |
| New user created | iitgpu/mailer → daemon → Resend | **each admin, no creds** | Live |
| Login notification | iitgpu/mailer → daemon → Resend | **user only** (new IP) | Live |
| Offboard notice | iitgpu/mailer → daemon → Resend | **user only** | Live |
| Sender address | `admin@gpu.indrajith.net` | — | Resend-verified domain |
| Mail log viewer | Admin panel → daemon → `/var/log/msmtp.log` | — | Live (gpusync in adm) |

### Groups (login node)

| Principal | Groups |
|-----------|--------|
| `gpusync` | gpusync gpuusers auditadmin **adm** |
| `slurmadmin` | slurmadmin auditadmin gpuadmins |
| `dahamadmin` | dahamadmin gpuusers gpuadmins |

### /shared layout

```
/shared/
├── data/ envs/ images/ jobs/ miniforge3/ models/ scripts/ templates/ tmp/
└── users/
    └── <per-user workspaces — one dir per active user>
```
