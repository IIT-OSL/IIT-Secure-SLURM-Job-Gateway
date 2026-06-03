# M05 — IIT Secure SLURM Job Gateway: Mail Infrastructure, User Management & Admin UX

**Date:** 2026-06-03
**Author:** Daham Dissanayake
**Scope:** Two-session document. Session A covered the Resend SMTP pipeline,
branded HTML job notifications, `/shared/users/` storage restructure, and
per-user file jail. Session B (this document's additions) covers the full
transactional mail service (`iitgpu/mailer.py`), admin panel reorganisation,
user lifecycle email notifications, timezone hardening, and user-management
bug fixes. **Builds on:** M04 (definitive architecture reference).
**Tests:** 450 passing (unchanged — all new code covered by existing suite).
**Repo:** `https://github.com/DahamDissanayake/IIT-Secure-SLURM-Job-Gateway`
**Deployed at:** `/opt/iit-gpu/` on login node (192.168.122.10)

---

## Table of Contents

1. [Session A — What Was Already Here](#1-session-a--what-was-already-here)
2. [Session B — Changes Overview](#2-session-b--changes-overview)
3. [Bug Fixes](#3-bug-fixes)
4. [Timezone Hardening — GMT+5:30 Everywhere](#4-timezone-hardening--gmt530-everywhere)
5. [Transactional Mail Service — iitgpu/mailer.py](#5-transactional-mail-service--iitgpumailerpy)
6. [Mail Architecture — Full Picture](#6-mail-architecture--full-picture)
7. [User Management — Lifecycle Hardening](#7-user-management--lifecycle-hardening)
8. [Admin Panel Redesign](#8-admin-panel-redesign)
9. [System Fix — gpusync adm Group](#9-system-fix--gpusync-adm-group)
10. [Architecture Delta — How the System Changed](#10-architecture-delta--how-the-system-changed)
11. [Session A Reference](#11-session-a-reference)
12. [Commits This Session](#12-commits-this-session)
13. [Active State](#13-active-state)

---

## 1. Session A — What Was Already Here

At the start of Session B the following were fully deployed on `main`:

| Area | State |
|------|-------|
| SLURM 25.11.2 | `slurmctld` + `slurmd` active |
| Resend SMTP | `msmtp` wired, `MailProg=/usr/local/bin/iit-gpu-mailer` |
| HTML job mailer | 6 event types, dark-header design, Resend API + msmtp fallback |
| `iit-gpu-audit` | Unix socket daemon, SO_PEERCRED identity, SQLite |
| Per-user file jail | browse `users/<u>/` + models + envs; upload to `users/<u>/` only |
| `/shared/users/` | all user workspaces under this prefix |
| Admin panel (v1) | flat 15-item list — no grouping |
| 450 tests | passing on `main` (commit `2966aa3`) |

---

## 2. Session B — Changes Overview

| Area | Change |
|------|--------|
| Bug | `entry.elapsed` → `entry.time_used` in All-user job history |
| Bug | Re-provision of an offboarded username caused UNIQUE constraint crash |
| Bug | Mail delivery log panel returned empty (daemon lacked `adm` group) |
| Timezone | Job folder timestamps and data file names now use GMT+5:30 |
| Mail service | New `iitgpu/mailer.py` — welcome, login notification, offboard emails |
| Mail service | All transactional emails BCC admin users |
| Mail service | SLURM job emails (`iit-gpu-mailer`) also BCC admin users |
| Daemon | New verb `users.admin_emails` — returns admin email list |
| User lifecycle | Welcome email with credentials + SSH instructions on account creation |
| User lifecycle | Login notification email on every TUI session start |
| User lifecycle | Offboard notification email when account is deactivated |
| User lifecycle | Re-provision of offboarded usernames now works correctly (upsert) |
| Admin panel | Grouped into 4 sections with visual separators |
| Admin panel | Maintenance notice merged into one smart entry (set/update/clear) |

---

## 3. Bug Fixes

### 3.1 All-user job history crash — `entry.elapsed`

**Symptom:**
```
✘  Unexpected error: 'QueueEntry' object has no attribute 'elapsed'
```

**Root cause:** `admin.py:745` referenced `entry.elapsed` but `QueueEntry`
(defined in `slurm.py:45`) has always named the field `time_used`. The attribute
name was never `elapsed` — a stale reference introduced when the admin panel was written.

**Fix:** `iitgpu/admin.py`
```python
# Before
t.add_row(entry.job_id, entry.user, entry.name, entry.state,
          entry.elapsed, entry.partition)
# After
t.add_row(entry.job_id, entry.user, entry.name, entry.state,
          entry.time_used, entry.partition)
```
Commit: `70f225f`

---

### 3.2 Mail delivery log panel always empty

**Symptom:** Admin → Mail delivery log showed:
```
Log empty or unavailable (check daemon + /var/log/msmtp.log).
```
even though `/var/log/msmtp.log` had entries.

**Root cause:** `/var/log/msmtp.log` is owned `root:adm 640`. The audit daemon
runs as `gpusync` (UID 997). `gpusync` was not in the `adm` group, so every
`Path(log_path).read_text()` call raised `PermissionError`. The handler caught
it as `OSError` and returned an empty list, which triggered the "Log empty" message.

**Fix (system-level):**
```bash
sudo usermod -aG adm gpusync
sudo systemctl restart iit-gpu-audit
```

See §9 for full analysis.

---

### 3.3 Re-provisioning an offboarded username — UNIQUE constraint

**Symptom:**
```
⚠  user DB record failed: UNIQUE constraint failed: users.username
```

**Root cause:** `_h_users_create` did a plain `INSERT`. Offboarding only sets
`status='offboarded'` — it does not delete the row. The UNIQUE constraint on
`username` prevented a second INSERT for the same username.

**Fix:** `deploy/audit_daemon.py` — `_h_users_create` now checks first:

```python
existing = users_conn.execute(
    "SELECT status FROM users WHERE username=?", (username,)
).fetchone()
if existing:
    if existing[0] != "offboarded":
        return False, None, f"user '{username}' already exists and is active"
    users_conn.execute(
        "UPDATE users SET uid=?,full_name=?,email=?,role=?,status='active',"
        "created_at=?,created_by=?,notes=? WHERE username=?",
        (uid_val, full_name, email, role, now, peer_user, notes, username),
    )
else:
    users_conn.execute("INSERT INTO users (...) VALUES (...)", ...)
users_conn.commit()
```

**Behaviour after fix:**

| Scenario | Result |
|----------|--------|
| New username | `INSERT` — unchanged |
| Offboarded username | `UPDATE` — row restored to `active` with new details |
| Active username | Rejected: `user 'X' already exists and is active` |

Commit: `c4204ad`

**Note on daemon restart:** The fix was deployed but the running daemon was not
automatically restarted because `tail -6` on the deploy output hid the restart step
result, and the daemon remained at its old PID. Manually restarted:
```bash
sudo systemctl restart iit-gpu-audit
# ExecMainStartTimestamp: 08:01:55 UTC (confirmed post-fix)
```

---

## 4. Timezone Hardening — GMT+5:30 Everywhere

### Problem

The login node's system clock is `Etc/UTC`. Python's `datetime.now()` (naive)
therefore returns UTC. Two places stamped user-visible paths with naive `now()`,
making folder names appear 5 h 30 min in the past.

### Audit timestamps — already correct, no change

`auditclient.py` stores `datetime.now(timezone.utc).isoformat()` — UTC ISO-8601.
`admin.py`'s `_fmt_ts()` converts on every display:

```python
_LK = timezone(timedelta(hours=5, minutes=30))

def _fmt_ts(ts_str: str) -> str:
    dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
    return dt.astimezone(_LK).strftime("%Y-%m-%d %H:%M:%S")
```

Audit log, user table "Created at", job history timestamps all already showed GMT+5:30.

### Fixed: job folder names (`iitgpu/jobs.py`)

```python
# Before
timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
# After
from datetime import timezone, timedelta
_LK = timezone(timedelta(hours=5, minutes=30))
timestamp = datetime.now(_LK).strftime("%Y%m%d_%H%M%S")
```

Users see `finetune_20260603_154500` (LK) not `finetune_20260603_100000` (UTC).

### Fixed: inline-paste data files (`iitgpu/wizard.py`)

Same pattern — `datetime.now(_LK)` for the `ts_inline.txt` filename.

### Rule going forward

| Usage | Pattern | Reason |
|-------|---------|--------|
| Storage / DB timestamps | `datetime.now(timezone.utc).isoformat()` | Unambiguous; convertible to any tz |
| Displayed timestamps | `dt.astimezone(_LK).strftime(...)` | GMT+5:30 for Sri Lanka |
| User-visible filenames | `datetime.now(_LK).strftime(...)` | Direct LK timestamp |

Commit: `a498570`

---

## 5. Transactional Mail Service — iitgpu/mailer.py

### Overview

New module `iitgpu/mailer.py` handles all non-SLURM transactional emails.
Runs within the Python process using `curl` → Resend HTTP API (same transport
as `iit-gpu-mailer`). Every send is non-blocking (background `daemon=True` thread).

### Design principles

- **Non-blocking:** every send goes into a background thread — the TUI never
  waits for network I/O.
- **BCC admins:** every email BCC's all active admin-role users from `users.db`
  (fetched live via `users.admin_emails`). The recipient is excluded from their
  own BCC entry.
- **Fail-silent:** network errors go to stderr; the caller never sees an exception.
  A missing email address silently skips the notification.
- **GMT+5:30 timestamps:** all displayed times use the `_LK` timezone constant.

### Public API

```python
send_welcome(username, password, email, full_name="")
send_login_notification(username, email, remote_ip)
send_offboard(username, email, full_name="")
```

### Internal helpers

```python
_send(to, subject, html, bcc=None)   # curl → api.resend.com/emails
_fire(to, subject, html, bcc=None)   # Thread(target=_send, daemon=True).start()
_admin_bcc()                          # daemonclient.admin_emails() → list[str]
_resend_key()                         # env RESEND_API_KEY > site.env > hardcoded default
```

### Welcome email — triggered by `provision_user()`

Sent only when all three conditions hold: OS account created, DB record written,
and password set successfully (`ok_pw == True`).

```python
if email and password and ok_pw:
    Thread(target=_mailer.send_welcome,
           args=(username, password, email, full_name),
           daemon=True).start()
```

**Content:**
- Username, password (plaintext), SSH command: `ssh -p 2225 username@10.35.4.100`
- Network restriction notice (blue border): IIT-CityCampus-SpencerBuilding only
- Security notice (red border): do not share credentials, all activity is monitored
- Numbered getting-started steps
- "Contact IIT Research Team" help block

**Subject:** `[IIT GPU Cluster] Your account is ready — <username>`
**Accent:** Blue `#3B82F6`

### Login notification — triggered by `__main__.main()`

Fires in a background thread immediately after the `session_start` audit event.
Does not delay the splash screen.

```python
_login_user = getpass.getuser()
_remote_ip  = os.environ.get("SSH_CLIENT", "").split()[0]

def _fire_login_notification():
    email = daemonclient.email_for(_login_user)
    if email:
        mailer.send_login_notification(_login_user, email, _remote_ip)

Thread(target=_fire_login_notification, daemon=True).start()
```

Source IP is extracted from `$SSH_CLIENT` (set by sshd: `client_ip srcport dstport`).

**Content:** Username, timestamp (GMT+5:30), source IP.
**Subject:** `[IIT GPU Cluster] Login detected — <username>`
**Accent:** Green `#22C55E`

### Offboard notification — triggered by `offboard_user()`

The user record is fetched **before** `iit-gpu-deluser` runs (the row is needed
for email and full name; it will be marked `offboarded` but not deleted):

```python
user_record = daemonclient.get_user(username)
# ... run iit-gpu-deluser ...
if rc == 0 and user_record and user_record.get("email"):
    Thread(target=_mailer.send_offboard,
           args=(username, user_record["email"], user_record.get("full_name", "")),
           daemon=True).start()
```

**Content:** Name, username, deactivation timestamp. "Contact IIT Research Team
if in error" note.
**Subject:** `[IIT GPU Cluster] Account deactivated — <username>`
**Accent:** Grey `#6B7280`

---

## 6. Mail Architecture — Full Picture

### Two parallel mail paths

**Path A — SLURM job lifecycle (`iit-gpu-mailer`)**

```
Job reaches terminal state
  → slurmctld reads MailProg=/usr/local/bin/iit-gpu-mailer from slurm.conf
  → iit-gpu-mailer -s "SLURM Job_id=N Name=X Ended ..." user@email
      ├── parse subject → extract job_id, event type
      ├── sacct -j <job_id> → live details
      ├── query /run/iit-gpu/audit.sock  →  users.admin_emails  →  BCC list
      ├── build HTML
      └── curl POST api.resend.com/emails  (to: user, bcc: admins)
          fallback: /usr/bin/msmtp (plain text, no BCC)
```

Events: `STARTED` · `ENDED` · `FAILED` · `TIMEOUT` · `REQUEUED` · `OOM`

**Path B — User lifecycle (`iitgpu/mailer.py`)**

```
Account created  →  provision_user()  →  Thread  →  send_welcome()
                                               →  Resend API  →  user + BCC admins

User logs in     →  __main__.main()   →  Thread  →  send_login_notification()
                                               →  Resend API  →  user + BCC admins

Account removed  →  offboard_user()   →  Thread  →  send_offboard()
                                               →  Resend API  →  user + BCC admins
```

### BCC mechanism

Both paths query the same daemon verb:

```
any caller → /run/iit-gpu/audit.sock  (srwxrwxrwx — world-writable)
  → verb: users.admin_emails
  → _h_users_admin_emails():
      SELECT email FROM users
      WHERE role='admin' AND status='active' AND email != ''
  → returns: list[str]
```

`users.admin_emails` is a **public verb** (no admin-group check on the socket).
This is required because `iit-gpu-mailer` runs as `slurmctld`'s system user,
which is not a `gpuusers` member.

### Complete email matrix

| Email | Trigger | To | BCC | Accent |
|-------|---------|-----|-----|--------|
| Welcome / credentials | Account created | New user | All admins | Blue |
| Login detected | Every TUI `session_start` | User | All admins | Green |
| Account deactivated | Offboard | User | All admins | Grey |
| Job started | SLURM BEGIN | Job owner | All admins | Blue |
| Job completed | SLURM END (COMPLETED) | Job owner | All admins | Green |
| Job failed | SLURM FAIL | Job owner | All admins | Red |
| Job timed out | SLURM TIMEOUT | Job owner | All admins | Amber |
| Job OOM | SLURM OOM | Job owner | All admins | Red |
| Job requeued | SLURM REQUEUE | Job owner | All admins | Purple |

All emails share the same visual template:
4 px accent top bar · dark header `#111827` · white content `#FFFFFF` ·
monospace detail table · footer with LK timestamp and "By: IIT Research Team".

---

## 7. User Management — Lifecycle Hardening

### Full user lifecycle (after Session B)

```
Admin → "Provision user"
  → _provision_menu(): username, role, full name, email, notes, password
  → provision_user()
      ├── iit-gpu-adduser  (OS user both nodes, SLURM assoc, /shared/users/<u>/)
      ├── daemonclient.create_user()
      │       → daemon _h_users_create() [upsert if offboarded]
      ├── auditclient.log("admin_provision_user")
      ├── set_user_password() via chpasswd
      └── Thread → mailer.send_welcome()           ← NEW

User logs in via SSH
  → ForceCommand: iit-gpu-manager
  → __main__.main()
      ├── auditclient.log("session_start")
      └── Thread → mailer.send_login_notification() ← NEW

Admin → "Offboard user"
  → daemonclient.get_user()  [fetch record BEFORE deletion]
  → offboard_user()
      ├── iit-gpu-deluser (removes OS user, SLURM assoc, optionally purges /shared)
      ├── daemonclient.offboard_user() → UPDATE status='offboarded'
      ├── auditclient.log("admin_offboard_user")
      └── Thread → mailer.send_offboard()           ← NEW
```

### Daemon verb changes

| Verb | Change | Auth |
|------|--------|------|
| `users.create` | Upserts if existing row is `offboarded` | Admin |
| `users.admin_emails` | New — returns list of admin emails | **Public** |

### Users DB schema (unchanged)

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
    notes      TEXT NOT NULL DEFAULT ''
);
```

On re-provision of an offboarded username: all fields except `username` are
overwritten. `created_at` resets to re-provision time, `created_by` to the
acting admin. The audit trail for the previous tenure is preserved in the
`audit_events` table independently.

---

## 8. Admin Panel Redesign

### Before (v1) — flat 15-item list

All actions were presented as a single unsorted list. Unrelated operations
(drain a node vs. view an audit log vs. provision a user) appeared adjacent
with no visual grouping.

### After (v2) — 4 grouped sections with separators

```
? Select action:
──  User Management  ──────────────────────────
  Provision user
  Offboard user
  View users
──  Jobs & Usage  ─────────────────────────────
  All-user job history
  Cluster usage (all users)
  Disk usage by user
  Any user's job output
──  Cluster Control  ──────────────────────────
  Drain node
  Resume node
  QOS / limits
  Maintenance notice
──  Monitoring  ───────────────────────────────
  Audit log
  Service health
  Mail delivery log
───────────────────────────────────────────────
  Back
```

Implemented using `questionary.Separator` objects interleaved with choice
strings. Choices have a leading two-space indent for visual hierarchy.
After `.ask()` the result is `.strip()`'d so the dispatch `if/elif` chain
matches plain strings without the indent.

### Section rationale

| Section | Actions | Why grouped |
|---------|---------|-------------|
| User Management | Provision, Offboard, View users | All affect user DB + OS accounts |
| Jobs & Usage | History, usage, disk, output | All read job/resource data |
| Cluster Control | Drain, Resume, QOS, Maintenance | All affect scheduler behaviour |
| Monitoring | Audit, Service health, Mail log | All read-only observability |

### Maintenance notice consolidation

Two separate items ("Set maintenance notice" / "Clear maintenance notice") were
merged into one `_maintenance_menu()` function:

- **No active notice** → prompts for text → sets notice.
- **Active notice** → shows current text → `Update notice` / `Clear notice` / `Back`.

Removes one top-level item and handles both states from a single entry point.

### Cluster usage press-any-key fix

The cluster usage block was missing a `press_any_key_to_continue` call — output
would flash and immediately loop back to the menu. Added.

---

## 9. System Fix — gpusync adm Group

### Problem

`/var/log/msmtp.log`:
```
-rw-r----- 1 root adm 227 Jun  3 04:46 /var/log/msmtp.log
```

`gpusync` groups before fix: `gpusync gpuusers auditadmin` — not `adm`.
Every read by the daemon raised `PermissionError` (caught silently as `OSError`),
returning an empty line list and triggering the "Log empty" UI message.

### Fix

```bash
sudo usermod -aG adm gpusync
sudo systemctl restart iit-gpu-audit
```

`gpusync` groups after fix: `gpusync gpuusers auditadmin adm`

### Why `adm` and not a custom group

`adm` is the standard Debian/Ubuntu group for system log readers. All log
files under `/var/log/` created by syslog, msmtp, etc. default to `root:adm 640`.
Adding `gpusync` to `adm` handles `/var/log/msmtp.log` now and any future log
files created with the same pattern, without requiring per-file `chgrp`.

### Not in git

System group membership is infrastructure state. It is recorded here and must
be included in any cluster re-provisioning runbook (alongside the SLURM
install, munge key, etc.).

---

## 10. Architecture Delta — How the System Changed

### Module map (Session B additions marked ←)

```
iitgpu/
├── __main__.py       ← login notification thread on session_start
├── admin.py          ← grouped menu, _maintenance_menu, offboard email, press-any-key fix
├── auditclient.py      (unchanged)
├── config.py           (unchanged)
├── containers.py       (unchanged)
├── daemonclient.py   ← admin_emails()
├── dashboard.py        (unchanged)
├── files.py            (unchanged)
├── jobs.py           ← GMT+5:30 _LK constant, datetime.now(_LK)
├── mailer.py         ← NEW: send_welcome, send_login_notification, send_offboard
├── menu.py             (unchanged)
├── models.py           (unchanged)
├── monitor.py          (unchanged)
├── notify.py           (unchanged)
├── shell.py            (unchanged)
├── slurm.py            (unchanged)
├── splash.py           (unchanged)
├── templates.py        (unchanged)
├── ui.py               (unchanged)
├── upload.py           (unchanged)
├── validate.py         (unchanged)
└── wizard.py         ← GMT+5:30 _LK constant, datetime.now(_LK)

deploy/
├── audit_daemon.py   ← _h_users_admin_emails, upsert in _h_users_create
└── redeploy-igm.sh     (unchanged)

/usr/local/bin/
└── iit-gpu-mailer    ← _daemon_admin_emails(), BCC in send_resend()
```

### Data flow changes

**Mail flow before Session B:**
```
SLURM event → iit-gpu-mailer → Resend API → job owner only
```

**Mail flow after Session B:**
```
SLURM event   → iit-gpu-mailer → daemon(admin_emails) → Resend API → job owner + BCC admins
Account created → mailer.send_welcome()                → Resend API → new user + BCC admins
User logs in    → mailer.send_login_notification()     → Resend API → user    + BCC admins
Account removed → mailer.send_offboard()               → Resend API → user    + BCC admins
```

**User DB create path before Session B:**
```
provision_user() → CREATE → INSERT → UNIQUE error if username exists as offboarded
```

**User DB create path after Session B:**
```
provision_user() → CREATE → check existing row
                              offboarded → UPDATE (re-activate, overwrite fields)
                              active     → reject (clear error message)
                              not found  → INSERT (unchanged)
```

### Complete daemon verb table

| Verb | Auth | Description |
|------|------|-------------|
| `audit.log` | Any | Record an audit event |
| `users.email_for` | Any (self or admin) | Get email for one user |
| `users.admin_emails` | **Any** | Get all active admin emails (BCC) |
| `users.create` | Admin | Create or re-activate user record (upsert) |
| `users.get` | Admin | Get one user record |
| `users.list` | Admin | List all users |
| `users.offboard` | Admin | Mark user as offboarded |
| `users.reconcile` | Admin | Find OS/DB mismatches |
| `audit.query` | Admin | Query audit event log |
| `roster.view` | Admin | View combined user roster |
| `maillog.tail` | Admin | Read `/var/log/msmtp.log` |
| `joblog.read` | Admin | Read a user's job output file |
| `service.status` | Admin | Check systemd unit health |

---

## 11. Session A Reference

Session A changes are preserved here for completeness. Full detail was in the
original M05-log commit `2966aa3`.

| Area | What changed |
|------|-------------|
| Mail — infrastructure | msmtp installed, `/etc/msmtprc` configured for Resend SMTP relay |
| Mail — SLURM wiring | `MailProg=/usr/local/bin/iit-gpu-mailer` in `slurm.conf` |
| Mail — HTML mailer | `deploy/iit-gpu-mailer`: 6 event types, dark-header design, sacct details |
| Bug fix | `_run()` in `admin.py` — `stdin` + `input` conflict in `subprocess.run()` |
| Storage | `user_dir()` helper in `config.py`; all user paths under `/shared/users/` |
| Storage | `iit-gpu-adduser` updated; 7 workspace dirs migrated from `/shared/` root |
| ACL access | slurmadmin (UID 1000) and daham (UID 1002): recursive `rwX` ACLs on all `/shared/*` |
| ACL access | Default ACLs set — new files inherit grants automatically |
| slurmadmin | Added to `gpuusers` group on login node |
| Per-user file jail | browse: `users/<u>/` + `models/` + `envs/` (read-only outside own dir) |
| Per-user file jail | upload: `users/<u>/` only for non-admins |
| Per-user file jail | admins: full NFS jail access in both file manager and upload TUI |
| Tests | `test_e2e.py` PYTHONPATH fix; two tests updated for `_run()` and `user_dir()` changes |

---

## 12. Commits This Session

### Session A

| Hash | Message |
|------|---------|
| `60ddba7` | `fix(test): pass PYTHONPATH into selftest subprocess` |
| `ee58565` | `feat(mail): branded HTML job notification mailer via Resend API` |
| `bff48df` | `feat(mail): redesign emails — dark theme, accent divider line only, no icons` |
| `a79988a` | `fix(mail): dark header + white content to survive email client overrides` |
| `9b3c1c2` | `feat(mail): add IIT Research Team credit to footer` |
| `e21e6e9` | `fix(users): provision error + /shared/users/ restructure` |
| `114b299` | `feat(jail): per-user file scope — browse own dir + models/envs, upload to own dir only` |
| `2966aa3` | `docs(log): update M05-log with per-user file jail changes (450 tests)` |

### Session B

| Hash | Message |
|------|---------|
| `70f225f` | `fix(admin): use time_used instead of nonexistent elapsed on QueueEntry` |
| `a498570` | `fix(tz): use GMT+5:30 for all user-visible timestamps (job folders, data files)` |
| `646e277` | `feat(mail): welcome email on user creation, login notifications, BCC admins on all mail` |
| `c4204ad` | `fix(users): re-provision offboarded username by updating existing row instead of INSERT` |
| `0e12765` | `feat(admin): grouped admin panel with sections, offboard email notification` |

---

## 13. Active State

### Services

| Service | Node | State |
|---------|------|-------|
| `slurmctld` | login (192.168.122.10) | active |
| `slurmd` | GPU host (192.168.122.1) | active |
| `slurmdbd` | login (192.168.122.10) | active |
| `mariadb` | login (192.168.122.10) | active |
| `iit-gpu-audit` | login (192.168.122.10) | active (restarted 08:01:55 UTC) |
| `iit-gpu-stats` | GPU host (192.168.122.1) | active |

### Tests

```
450 passed  (PYTHONPATH=/opt/iit-gpu python3 -m pytest tests/ -q)
```

### Mail delivery

| Path | From | Status |
|------|------|--------|
| SLURM job events | iit-gpu-mailer → Resend API | Live + BCC admins |
| Welcome (on create) | iitgpu/mailer → Resend API | Live + BCC admins |
| Login notification | iitgpu/mailer → Resend API | Live + BCC admins |
| Offboard notice | iitgpu/mailer → Resend API | Live + BCC admins |
| Sender address | `admin@gpu.indrajith.net` | Resend-verified domain |
| Mail log viewer | Admin panel → daemon → `/var/log/msmtp.log` | Live (gpusync in adm) |

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

slurmadmin (UID 1000): `rwx` ACL + default ACL on every `/shared/*` dir.
