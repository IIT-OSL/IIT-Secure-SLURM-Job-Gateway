# M05 Fixes Runbook — Manual Steps

Steps that require root, a live service restart, or GPU-host access.
Run these **in order** after each Phase is merged to main and redeployed.

---

## Phase 1 — Credential Safety

### [LOGIN] Rotate the Resend API key

The key `re_Fx3dzHkn_…` was hardcoded in source and is now in git history.
History has been rewritten (filter-repo, force-pushed), but the key must be
considered compromised.

```bash
# 1. Go to https://resend.com/api-keys
#    Delete the old key: re_Fx3dzHkn_MGK594BR8LxsQWBqt8PSYfj7

# 2. Create a new key with the same sending domain permissions.

# 3. Add it to site.env on the login node (not in source):
echo 'RESEND_API_KEY=re_YOUR_NEW_KEY_HERE' >> /opt/iit-gpu/deploy/site.env

# 4. Restart the audit daemon so mailer.py picks up the new key:
sudo systemctl restart iit-gpu-audit
```

### [LOGIN] Rotate the GitHub token

The GitHub PAT `ghp_REDACTED_ROTATE_THIS_TOKEN` appeared in the
filter-repo output (it was embedded in the remote URL). Treat as compromised.

```bash
# 1. Go to https://github.com/settings/tokens
#    Revoke: ghp_REDACTED_ROTATE_THIS_TOKEN

# 2. Create a new fine-grained token with repo write access.

# 3. Update the remote URL on the login node:
git -C /home/slurmadmin/IIT-Secure-SLURM-Job-Gateway remote set-url origin \
  https://DahamDissanayake:NEW_TOKEN_HERE@github.com/DahamDissanayake/IIT-Secure-SLURM-Job-Gateway.git
```

### [LOGIN] Restart audit daemon after deploy

Phase 1 migrates the `users.db` schema (adds `must_change_pw` column). The
daemon picks this up automatically at startup via the `ALTER TABLE … ADD COLUMN`
migration, but only after a restart.

```bash
sudo systemctl restart iit-gpu-audit
# Verify:
sudo systemctl show iit-gpu-audit --property=ExecMainStartTimestamp,ActiveState
```

### [LOGIN] Optional — system-level password expiry enforcement

The Phase 1 implementation uses a TUI-level `must_change_pw` flag in `users.db`.
This is enforced when users enter via `iit-gpu-manager` (ForceCommand). It does
not prevent a user from bypassing the TUI via sftp or scp.

To add a system-level `chage -d 0` enforcement (forces PAM to require a password
change at every login method), you must first enable SSH challenge-response:

```bash
# 1. On the login node, edit /etc/ssh/sshd_config:
sudo sed -i 's/ChallengeResponseAuthentication no/ChallengeResponseAuthentication yes/' \
  /etc/ssh/sshd_config

# 2. Restart sshd (test with a second SSH session first — do NOT close your current one):
sudo systemctl restart sshd

# 3. Add chage to the gpuadmins sudoers so provision_user() can call it:
sudo visudo -f /etc/sudoers.d/gpuadmins
# Add /usr/bin/chage to the NOPASSWD list, e.g.:
#   slurmadmin ALL=(root) NOPASSWD: /usr/bin/scontrol update *, ... /usr/bin/chage

# 4. After these two changes are in place, the code in provision_user() can
#    additionally call:
#      sudo -n /usr/bin/chage -d 0 <username>
#    to enforce the change at the OS level as well.
```

**Warning:** Do not run `chage -d 0` on existing accounts without first enabling
`ChallengeResponseAuthentication yes`. With the current sshd config
(`ChallengeResponseAuthentication no`), an expired password blocks SSH login
entirely — the user cannot connect at all, not even to change it.

---

## Future phases (placeholders)

- **Phase 2** — API key from env only, curl key not in argv
- **Phase 3** — Must-deliver mail (welcome/offboard synchronous)
- **Phase 5** — Redeploy daemon restart verification
- **Phase 5** — Re-provision workspace prompt
- **Phase 6** — sbatch validator sudoers if needed
- **Phase 7** — Login-notice deduplication (last-seen IPs)

---

## Mail & file-picker fixes (2026-06-04, commits 265f69d + 74e33f3)

Three post-deploy bug fixes. Only the first needs a **manual step** (a group
change + `slurmctld` restart); the other two are code-only and ship with the
normal `redeploy-igm.sh`.

### [LOGIN] Job-status email never delivered — add `slurm` to `gpusync` (REQUIRED)

**Symptom:** login/welcome mail worked, but SLURM job notices (BEGIN/END/FAIL)
were never received.

**Root cause:** `slurmctld` runs `MailProg` (`/usr/local/bin/iit-gpu-mailer`) as
**`SlurmUser` = `slurm`**, *not* root. `slurm` was only in `slurm,gpuusers`, so
it could **not** read the Resend key in `secrets.env` (`0640 root:gpusync`). The
mailer then fell back to msmtp, which also failed twice: `slurm` cannot read
`/etc/msmtprc` (`0600 root:root`), **and** the fallback passed `msmtp -s …`
(`-s` is a mailx/sendmail option msmtp rejects: `invalid option -- 's'`). Both
paths failed silently.

`deploy/install.sh` and `deploy/redeploy-igm.sh` now add the membership
automatically, and `redeploy-igm.sh` restarts `slurmctld` only when it actually
adds it. On a host that pre-dates this change, apply it once by hand:

```bash
# Add the SlurmUser to gpusync so MailProg can read the send-only Resend key:
sudo usermod -aG gpusync slurm

# slurmctld caches its supplementary groups at start — restart so it picks up
# gpusync (running jobs are unaffected; this only restarts the controller):
sudo systemctl restart slurmctld

# Verify membership and that the controller is back up:
id -nG slurm | tr ' ' '\n' | grep -qx gpusync && echo "slurm in gpusync: OK"
systemctl is-active slurmctld
```

**Verify end-to-end** (raises controller debug briefly, then restores it):

```bash
sudo scontrol setdebug debug2
sbatch --wrap 'hostname; sleep 2' \
  --partition=gpu --mail-user=you@example.com --mail-type=BEGIN,END
# Watch for: MailProg output was 'iit-gpu-mailer: sent to … — HTTP 200'
sudo journalctl -u slurmctld --since '-1 min' | grep -i 'mail\|MailProg'
sudo scontrol setdebug info
```

> **GOTCHA:** MailProg runs as `slurm`, not root. Any secret or config it needs
> must be readable by the `slurm` user. The mailer's old "runs as root" comment
> was wrong and has been corrected.

> **NOTE:** `redeploy-igm.sh` re-execs from a pre-pull temp copy of itself, so a
> brand-new block added to that script first runs on the *next* deploy. The live
> `slurm`-in-`gpusync` membership above persists regardless.

### Code-only (no manual step) — file-picker scoping

- **Job wizard data/script picker (`iitgpu/wizard.py`):** `_browse_data_folder`
  / `_browse_script` now take a `jail` predicate and the wizard opens regular
  users in their own `shared/users/<user>` area (admins keep the full NFS jail),
  instead of starting at `/shared` and falling back there when the user dir was
  missing.
- **"Upload data" picker (`iitgpu/upload.py` `run_upload`):** always scopes to
  the user's own `shared/users/<username>` folder for **everyone, admins
  included** (it no longer lists every top-level `/shared` dir, where selecting a
  non-writable parent like `/shared/users` failed with "Could not create or
  access"). Admins who need other `/shared` locations use *Browse my files*.

Picked up by the normal redeploy:

```bash
bash /opt/iit-gpu/deploy/redeploy-igm.sh
```

---

## Security hardening (post-review) — manual steps

### [LOGIN] Create the daemon-only secrets file (C1)

The Resend API key must NOT live in site.env (group-readable by all users).
Move it into secrets.env, readable only by root + gpusync:

```bash
sudo cp /opt/iit-gpu/deploy/secrets.env.example /opt/iit-gpu/deploy/secrets.env
sudoedit /opt/iit-gpu/deploy/secrets.env          # set RESEND_API_KEY=...
sudo chown root:gpusync /opt/iit-gpu/deploy/secrets.env
sudo chmod 640 /opt/iit-gpu/deploy/secrets.env

# Remove the key from site.env (it is now ignored there):
sudo sed -i '/^RESEND_API_KEY=/d' /opt/iit-gpu/deploy/site.env

# Restart the daemon so it loads the key:
sudo systemctl restart iit-gpu-audit

# Verify a regular user CANNOT read it:
sudo -u <some_gpuuser> cat /opt/iit-gpu/deploy/secrets.env   # must be Permission denied
```

### [LOGIN] Tighten the working-copy site.env (L5)

```bash
sudo chmod 600 /home/slurmadmin/IIT-Secure-SLURM-Job-Gateway/deploy/site.env
```

### Notes
- All TUI mail now flows through the daemon's `mail.send` verb; the key never
  enters a user or admin process.
- The SLURM `iit-gpu-mailer` (MailProg) runs as **`SlurmUser` = `slurm`**, not
  root, and reads secrets.env directly — which requires `slurm` to be in the
  `gpusync` group (see "Mail & file-picker fixes" above).
- `users.admin_emails` is now restricted to admins + root (was world-readable).
