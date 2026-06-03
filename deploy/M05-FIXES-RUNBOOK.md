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
