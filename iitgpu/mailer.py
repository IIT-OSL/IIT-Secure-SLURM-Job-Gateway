# iitgpu/mailer.py
"""Transactional email built client-side, SENT by the audit daemon.

The Resend API key lives only on the daemon (gpusync, secrets.env 0640
root:gpusync). No regular-user or admin process ever reads the key — they hand
the built message to the daemon's `mail.send` verb. The daemon enforces that
non-admin callers can only mail their own registered address (anti-relay) and
performs new-IP dedup for login notices. See C1/M2/M3 in the security review.
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from threading import Thread


def _cluster_tz():
    try:
        from iitgpu.config import cluster_tz
        return cluster_tz()
    except Exception:
        return timezone(timedelta(hours=5, minutes=30))


def _cluster_name() -> str:
    try:
        from iitgpu.config import _get
        return _get("CLUSTER_NAME", "IIT GPU Cluster")
    except Exception:
        return "IIT GPU Cluster"


def _cluster_location() -> str:
    try:
        from iitgpu.config import _get
        return _get("CLUSTER_LOCATION", "IIT-CityCampus-SpencerBuilding")
    except Exception:
        return "IIT-CityCampus-SpencerBuilding"


def _now_lk() -> str:
    return datetime.now(_cluster_tz()).strftime("%d %b %Y  %H:%M")


def _daemon_mail(to: str, subject: str, html: str,
                 bcc: list[str] | None = None,
                 kind: str = "generic", ip: str = "") -> tuple[bool, str]:
    """Hand a built message to the daemon to send. Returns (ok, message)."""
    try:
        from iitgpu.auditclient import daemon_request
        resp = daemon_request("mail.send", {
            "to": to, "subject": subject, "html": html,
            "bcc": bcc or [], "kind": kind, "ip": ip,
        }, timeout=25.0)
    except Exception as exc:
        return False, str(exc)
    if resp.get("ok"):
        return True, "sent"
    return False, resp.get("error", "mail failed")


def _send_sync(to: str, subject: str, html: str,
               bcc: list[str] | None = None,
               kind: str = "generic") -> tuple[bool, str]:
    """Must-deliver send via the daemon. Returns (success, message)."""
    return _daemon_mail(to, subject, html, bcc, kind=kind)


def _fire(to: str, subject: str, html: str, bcc: list[str] | None = None,
          kind: str = "generic", ip: str = "") -> None:
    """Best-effort non-blocking send (non-daemon thread, survives TUI exit)."""
    Thread(target=_daemon_mail, args=(to, subject, html, bcc, kind, ip),
           daemon=False).start()


def send_welcome(username: str, email: str, full_name: str = "",
                 password: str = "") -> tuple[bool, str]:
    """Send welcome email with the user's initial password. No admin BCC —
    this email is for the user only. The password is shown so the user can
    log in themselves; they are forced to change it on first login."""
    import html as _html
    from iitgpu.config import load_config
    cfg  = load_config()
    host = cfg.gateway_host
    port = cfg.gateway_port

    display_name = full_name or username
    ssh_cmd      = f"ssh -p {port} {username}@{host}"
    subject      = f"[{_cluster_name()}] Your account is ready — {username}"

    # Prominent credentials block — the user logs in with this plaintext
    # password, then is forced to change it. Rendered only when a password
    # was set during provisioning.
    password_block = ""
    if password:
        password_block = f"""
        <tr><td bgcolor="#FFFFFF" style="background:#FFFFFF;padding:8px 32px 4px">
          <div style="border:1px solid #E5E7EB;border-radius:8px;overflow:hidden">
            <div style="background:#111827;padding:12px 20px">
              <p style="margin:0;color:#F9FAFB;font-size:11px;font-weight:700;letter-spacing:1.2px;text-transform:uppercase">Your Login Password</p>
            </div>
            <div style="background:#F9FAFB;padding:20px">
              <p style="margin:0 0 10px;color:#6B7280;font-size:12px;line-height:1.5">Use this password the first time you log in:</p>
              <div style="background:#FFFFFF;border:1px dashed #9CA3AF;border-radius:6px;padding:14px 18px;text-align:center">
                <span style="color:#111827;font-size:20px;font-weight:700;letter-spacing:1px;font-family:'SF Mono',SFMono-Regular,Consolas,monospace;word-break:break-all">{_html.escape(password)}</span>
              </div>
            </div>
          </div>
        </td></tr>

        <tr><td bgcolor="#FFFFFF" style="background:#FFFFFF;padding:10px 32px 4px">
          <div style="border-left:3px solid #EF4444;padding:14px 18px;background:#FEF2F2">
            <p style="margin:0 0 6px;color:#B91C1C;font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:0.8px">⚠ Reset Your Password Immediately</p>
            <p style="margin:0;color:#991B1B;font-size:13px;line-height:1.7">This is a temporary password. <strong>As soon as you log in, you will be required to set a new password — do this right away.</strong> Do not share this password with anyone, and do not reuse it on other systems.</p>
          </div>
        </td></tr>"""

    html = f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#F4F4F5;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#F4F4F5;padding:40px 0">
    <tr><td align="center">
      <table width="580" cellpadding="0" cellspacing="0" style="max-width:580px;width:100%">

        <tr><td bgcolor="#3B82F6" style="background:#3B82F6;height:4px;font-size:0;line-height:0">&nbsp;</td></tr>

        <tr><td bgcolor="#111827" style="background:#111827;padding:28px 32px 26px">
          <p style="margin:0 0 20px;color:#4B5563;font-size:11px;font-weight:600;letter-spacing:2px;text-transform:uppercase">{_cluster_name()}</p>
          <h1 style="margin:0 0 8px;color:#F9FAFB;font-size:22px;font-weight:600;letter-spacing:-0.3px;line-height:1.3">Welcome, {display_name}</h1>
          <p style="margin:0;color:#9CA3AF;font-size:14px;line-height:1.6">Your GPU cluster account has been created and is ready to use.</p>
        </td></tr>

        <tr><td bgcolor="#FFFFFF" style="background:#FFFFFF;padding:28px 32px 8px">
          <p style="margin:0 0 16px;color:#9CA3AF;font-size:10px;font-weight:700;letter-spacing:2px;text-transform:uppercase">Account Details</p>
          <table width="100%" cellpadding="0" cellspacing="0">
            <tr>
              <td style="padding:10px 0;color:#6B7280;font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:0.8px;width:120px;border-bottom:1px solid #F3F4F6">Username</td>
              <td style="padding:10px 0 10px 20px;color:#111827;font-size:13px;font-family:'SF Mono',SFMono-Regular,Consolas,monospace;border-bottom:1px solid #F3F4F6">{username}</td>
            </tr>
            <tr>
              <td style="padding:10px 0;color:#6B7280;font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:0.8px;width:120px;border-bottom:1px solid #F3F4F6">SSH Command</td>
              <td style="padding:10px 0 10px 20px;color:#111827;font-size:13px;font-family:'SF Mono',SFMono-Regular,Consolas,monospace;border-bottom:1px solid #F3F4F6">{ssh_cmd}</td>
            </tr>
          </table>
        </td></tr>
{password_block}
        <tr><td bgcolor="#FFFFFF" style="background:#FFFFFF;padding:4px 32px 28px">
          <div style="margin-top:20px;border-left:3px solid #F59E0B;padding:14px 18px;background:#FFFBEB">
            <p style="margin:0 0 6px;color:#92400E;font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:0.8px">First Login — Password Change Required</p>
            <p style="margin:0;color:#78350F;font-size:13px;line-height:1.7">Log in with the temporary password shown above. <strong>The system will immediately prompt you to set a new personal password</strong> — choose a strong one you don't use anywhere else, and reset it the moment you log in.</p>
          </div>

          <div style="margin-top:14px;border-left:3px solid #3B82F6;padding:14px 18px;background:#EFF6FF">
            <p style="margin:0 0 6px;color:#1D4ED8;font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:0.8px">Network Access Restriction</p>
            <p style="margin:0;color:#1E40AF;font-size:13px;line-height:1.7">You can <strong>only</strong> connect from within the <strong>{_cluster_location()}</strong> network. Access from any other network is blocked.</p>
          </div>

          <div style="margin-top:14px;border-left:3px solid #EF4444;padding:14px 18px;background:#FEF2F2">
            <p style="margin:0 0 6px;color:#B91C1C;font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:0.8px">Security Notice</p>
            <p style="margin:0;color:#991B1B;font-size:13px;line-height:1.7"><strong>Do not share your credentials.</strong> Every upload, download, and job submission from your account is logged and monitored. Sharing your account violates cluster policy.</p>
          </div>

          <div style="margin-top:24px">
            <p style="margin:0 0 10px;color:#111827;font-size:13px;font-weight:600">Getting started</p>
            <ol style="margin:0;padding-left:20px;color:#374151;font-size:13px;line-height:2.2">
              <li>Connect to the <strong>{_cluster_location()}</strong> Wi-Fi or wired network.</li>
              <li>Open a terminal and run:<br>
                <code style="display:inline-block;margin-top:4px;background:#F3F4F6;padding:5px 10px;border-radius:4px;font-family:'SF Mono',Consolas,monospace;font-size:12px;color:#111827">{ssh_cmd}</code>
              </li>
              <li>Enter your initial password (shown in Account Details above) when prompted.</li>
              <li>You will immediately be asked to set a new personal password.</li>
              <li>The GPU Manager interface will launch automatically after that.</li>
            </ol>
          </div>

          <div style="margin-top:24px;padding:16px 20px;background:#F9FAFB;border-radius:6px;border:1px solid #E5E7EB">
            <p style="margin:0 0 4px;color:#111827;font-size:13px;font-weight:600">Need help?</p>
            <p style="margin:0;color:#6B7280;font-size:13px;line-height:1.6">For assistance with your account, job submissions, or any cluster questions, contact the <strong>IIT Research Team</strong>.</p>
          </div>
        </td></tr>

        <tr><td bgcolor="#F4F4F5" style="background:#F4F4F5;padding:18px 32px;border-top:1px solid #E4E4E7">
          <table width="100%" cellpadding="0" cellspacing="0">
            <tr>
              <td style="color:#A1A1AA;font-size:11px">{_cluster_name()}&nbsp;&middot;&nbsp;{_now_lk()} (GMT+5:30)</td>
              <td align="right" style="color:#A1A1AA;font-size:11px;font-family:monospace">iit-gpu-manager</td>
            </tr>
            <tr><td colspan="2" style="padding-top:4px;color:#A1A1AA;font-size:11px">By: IIT Research Team</td></tr>
          </table>
        </td></tr>

      </table>
    </td></tr>
  </table>
</body>
</html>"""

    # No BCC: welcome email is private to the recipient — never share with other admins.
    return _send_sync(email, subject, html, bcc=None, kind="welcome")


def send_offboard(username: str, email: str, full_name: str = "") -> tuple[bool, str]:
    # Goes only to the offboarded user — admins are not BCC'd on user-facing mail.
    display_name = full_name or username
    subject      = f"[{_cluster_name()}] Account deactivated — {username}"

    html = f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#F4F4F5;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#F4F4F5;padding:40px 0">
    <tr><td align="center">
      <table width="580" cellpadding="0" cellspacing="0" style="max-width:580px;width:100%">

        <tr><td bgcolor="#6B7280" style="background:#6B7280;height:4px;font-size:0;line-height:0">&nbsp;</td></tr>

        <tr><td bgcolor="#111827" style="background:#111827;padding:28px 32px 26px">
          <p style="margin:0 0 20px;color:#4B5563;font-size:11px;font-weight:600;letter-spacing:2px;text-transform:uppercase">{_cluster_name()}</p>
          <h1 style="margin:0 0 8px;color:#F9FAFB;font-size:22px;font-weight:600;letter-spacing:-0.3px;line-height:1.3">Account Deactivated</h1>
          <p style="margin:0;color:#9CA3AF;font-size:14px;line-height:1.6">Your {_cluster_name()} account has been deactivated by an administrator.</p>
        </td></tr>

        <tr><td bgcolor="#FFFFFF" style="background:#FFFFFF;padding:28px 32px 8px">
          <p style="margin:0 0 16px;color:#9CA3AF;font-size:10px;font-weight:700;letter-spacing:2px;text-transform:uppercase">Account Details</p>
          <table width="100%" cellpadding="0" cellspacing="0">
            <tr>
              <td style="padding:10px 0;color:#6B7280;font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:0.8px;width:120px;border-bottom:1px solid #F3F4F6">Name</td>
              <td style="padding:10px 0 10px 20px;color:#111827;font-size:13px;border-bottom:1px solid #F3F4F6">{display_name}</td>
            </tr>
            <tr>
              <td style="padding:10px 0;color:#6B7280;font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:0.8px;width:120px;border-bottom:1px solid #F3F4F6">Username</td>
              <td style="padding:10px 0 10px 20px;color:#111827;font-size:13px;font-family:'SF Mono',monospace;border-bottom:1px solid #F3F4F6">{username}</td>
            </tr>
            <tr>
              <td style="padding:10px 0;color:#6B7280;font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:0.8px;width:120px;border-bottom:1px solid #F3F4F6">Deactivated at</td>
              <td style="padding:10px 0 10px 20px;color:#111827;font-size:13px;font-family:'SF Mono',monospace;border-bottom:1px solid #F3F4F6">{_now_lk()} (GMT+5:30)</td>
            </tr>
          </table>
        </td></tr>

        <tr><td bgcolor="#FFFFFF" style="background:#FFFFFF;padding:4px 32px 28px">
          <div style="margin-top:16px;border-left:3px solid #6B7280;padding:14px 18px;background:#F9FAFB">
            <p style="margin:0;color:#374151;font-size:13px;line-height:1.7">Your SSH access and all cluster resources have been revoked. If you believe this was done in error, contact the <strong>IIT Research Team</strong>.</p>
          </div>
        </td></tr>

        <tr><td bgcolor="#F4F4F5" style="background:#F4F4F5;padding:18px 32px;border-top:1px solid #E4E4E7">
          <table width="100%" cellpadding="0" cellspacing="0">
            <tr>
              <td style="color:#A1A1AA;font-size:11px">{_cluster_name()}&nbsp;&middot;&nbsp;{_now_lk()} (GMT+5:30)</td>
              <td align="right" style="color:#A1A1AA;font-size:11px;font-family:monospace">iit-gpu-manager</td>
            </tr>
            <tr><td colspan="2" style="padding-top:4px;color:#A1A1AA;font-size:11px">By: IIT Research Team</td></tr>
          </table>
        </td></tr>

      </table>
    </td></tr>
  </table>
</body>
</html>"""

    return _send_sync(email, subject, html, bcc=None, kind="offboard")


def send_login_notification(username: str, email: str, remote_ip: str) -> None:
    subject = f"[{_cluster_name()}] Login detected — {username}"

    html = f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#F4F4F5;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#F4F4F5;padding:40px 0">
    <tr><td align="center">
      <table width="580" cellpadding="0" cellspacing="0" style="max-width:580px;width:100%">

        <tr><td bgcolor="#22C55E" style="background:#22C55E;height:4px;font-size:0;line-height:0">&nbsp;</td></tr>

        <tr><td bgcolor="#111827" style="background:#111827;padding:28px 32px 26px">
          <p style="margin:0 0 20px;color:#4B5563;font-size:11px;font-weight:600;letter-spacing:2px;text-transform:uppercase">{_cluster_name()}</p>
          <h1 style="margin:0 0 8px;color:#F9FAFB;font-size:22px;font-weight:600;letter-spacing:-0.3px;line-height:1.3">New login to your account</h1>
          <p style="margin:0;color:#9CA3AF;font-size:14px;line-height:1.6">A session was started on the {_cluster_name()} under your credentials.</p>
        </td></tr>

        <tr><td bgcolor="#FFFFFF" style="background:#FFFFFF;padding:28px 32px 8px">
          <p style="margin:0 0 16px;color:#9CA3AF;font-size:10px;font-weight:700;letter-spacing:2px;text-transform:uppercase">Session Details</p>
          <table width="100%" cellpadding="0" cellspacing="0">
            <tr>
              <td style="padding:10px 0;color:#6B7280;font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:0.8px;width:120px;border-bottom:1px solid #F3F4F6">User</td>
              <td style="padding:10px 0 10px 20px;color:#111827;font-size:13px;font-family:'SF Mono',monospace;border-bottom:1px solid #F3F4F6">{username}</td>
            </tr>
            <tr>
              <td style="padding:10px 0;color:#6B7280;font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:0.8px;width:120px;border-bottom:1px solid #F3F4F6">Time</td>
              <td style="padding:10px 0 10px 20px;color:#111827;font-size:13px;font-family:'SF Mono',monospace;border-bottom:1px solid #F3F4F6">{_now_lk()} (GMT+5:30)</td>
            </tr>
            <tr>
              <td style="padding:10px 0;color:#6B7280;font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:0.8px;width:120px;border-bottom:1px solid #F3F4F6">Source IP</td>
              <td style="padding:10px 0 10px 20px;color:#111827;font-size:13px;font-family:'SF Mono',monospace;border-bottom:1px solid #F3F4F6">{remote_ip or "local"}</td>
            </tr>
          </table>
        </td></tr>

        <tr><td bgcolor="#FFFFFF" style="background:#FFFFFF;padding:4px 32px 28px">
          <div style="margin-top:16px;padding:14px 18px;background:#F0FDF4;border-left:3px solid #22C55E">
            <p style="margin:0;color:#166534;font-size:13px;line-height:1.7">If this was you, no action is needed. If you did not log in, contact the <strong>IIT Research Team</strong> immediately.</p>
          </div>
        </td></tr>

        <tr><td bgcolor="#F4F4F5" style="background:#F4F4F5;padding:18px 32px;border-top:1px solid #E4E4E7">
          <table width="100%" cellpadding="0" cellspacing="0">
            <tr>
              <td style="color:#A1A1AA;font-size:11px">{_cluster_name()}&nbsp;&middot;&nbsp;{_now_lk()} (GMT+5:30)</td>
              <td align="right" style="color:#A1A1AA;font-size:11px;font-family:monospace">iit-gpu-manager</td>
            </tr>
            <tr><td colspan="2" style="padding-top:4px;color:#A1A1AA;font-size:11px">By: IIT Research Team</td></tr>
          </table>
        </td></tr>

      </table>
    </td></tr>
  </table>
</body>
</html>"""

    # Login notice: daemon forces recipient to the caller's own address and does
    # new-IP dedup server-side (only sends when remote_ip is unseen).
    _fire(email, subject, html, bcc=None, kind="login", ip=remote_ip or "local")
