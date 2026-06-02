# deploy/mail-setup.md — Job lifecycle email via msmtp + Resend SMTP

**Operator steps only — no secrets go in code or git.**
Run on the **login node** as root.

---

## What this gives you

When a registered user submits a job, the gateway auto-inserts `--mail-user` and
`--mail-type=BEGIN,END,FAIL,REQUEUE,TIME_LIMIT` into the sbatch script using the
email address from `users.db`.  SLURM invokes `MailProg` (`msmtp`) to relay those
notifications via Resend's SMTP endpoint.  No poll-loop, no daemon involvement —
it is standard SLURM mail, relayed by a local MTA.

---

## 1. Create a Resend account and SMTP key

1. Sign in at <https://resend.com>.
2. **Domains → Add Domain** — verify your cluster's sending domain
   (e.g. `iit-gpu.example.edu`).  Follow the DNS records instructions.
3. **API Keys → Create API Key** — scope: *Sending access* only.
   Copy the key (`re_...`); you will need it once below and never again.

---

## 2. Install msmtp

```bash
apt-get install -y msmtp msmtp-mta
```

`msmtp-mta` provides `/usr/sbin/sendmail` as a symlink to msmtp, which is what
SLURM's `MailProg` invokes.

---

## 3. Write `/etc/msmtprc`  (root `0600`)

```bash
cat > /etc/msmtprc <<'EOF'
# Global defaults
defaults
tls on
tls_starttls off
auth on

# Resend SMTP relay
account resend
host smtp.resend.com
port 465
from noreply@YOUR-VERIFIED-DOMAIN
user resend
password re_YOUR_API_KEY_HERE

account default : resend
EOF
chmod 0600 /etc/msmtprc
```

Replace `YOUR-VERIFIED-DOMAIN` and `re_YOUR_API_KEY_HERE` with your actual values.
**This file must never be committed to git.**

---

## 4. Wire SLURM to use msmtp

Add to `/etc/slurm/slurm.conf` (or `slurm.conf.d/mail.conf`):

```
MailProg=/usr/bin/msmtp
```

Then reload SLURM:

```bash
scontrol reconfigure
```

---

## 5. Smoke-test the pipeline

```bash
# Send a test email as root
echo "Subject: test" | msmtp --debug someone@example.com

# Submit a quick job as a registered user and watch the mail log
tail -f /var/log/msmtp.log
```

The admin panel → **Mail delivery log** tails this file in real time.

---

## 6. Configure `NOTIFY_MAIL_TYPES` (optional)

The default mail-type set is `BEGIN,END,FAIL,REQUEUE,TIME_LIMIT`.  To change it
cluster-wide, set in `deploy/site.env`:

```
NOTIFY_MAIL_TYPES=END,FAIL
```

Users cannot override this; it applies to every job submitted through the gateway.

---

## 7. Checklist

- [ ] `/etc/msmtprc` exists, mode `0600`, owner `root`
- [ ] `msmtp --debug` to a real address succeeds
- [ ] `slurm.conf` has `MailProg=/usr/bin/msmtp` and `scontrol reconfigure` ran
- [ ] Admin panel → Mail delivery log shows entries after a test submission
- [ ] `/var/log/msmtp.log` is not world-readable (`chmod 0640`, group `adm` or `slurmadmin`)
