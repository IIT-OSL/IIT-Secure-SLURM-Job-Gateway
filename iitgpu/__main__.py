# iitgpu/__main__.py
from __future__ import annotations
import argparse
import os
import signal
import sys


def _clean_exit(signum, frame):
    from iitgpu import auditclient
    auditclient.log("signal_exit", detail=f"signal={signum}")
    sys.exit(0)


def _ignore_tstp(signum, frame):
    pass


def _install_signal_handlers() -> None:
    signal.signal(signal.SIGINT, _clean_exit)
    if hasattr(signal, "SIGQUIT"):
        signal.signal(signal.SIGQUIT, _clean_exit)
    if hasattr(signal, "SIGTSTP"):
        signal.signal(signal.SIGTSTP, _ignore_tstp)


def _run_selftest() -> int:
    import importlib
    import tempfile
    from pathlib import Path

    failures: list[str] = []

    def check(name: str, condition: bool) -> None:
        print(f"  [{'PASS' if condition else 'FAIL'}] {name}")
        if not condition:
            failures.append(name)

    print("\n=== IIT-GPU-Manager Selftest ===\n")

    with tempfile.TemporaryDirectory() as td:
        os.environ["NFS_ROOT"] = td
        import iitgpu.validate as v
        importlib.reload(v)

        real_file = Path(td) / "ok.txt"
        real_file.write_text("x")
        check("in_jail accepts file under root", v.in_jail(str(real_file)))
        check("in_jail rejects /etc/shadow", not v.in_jail("/etc/shadow"))
        check("in_jail rejects ../etc escape", not v.in_jail(td + "/../etc"))
        check("clamp_int caps 9999 to MAX_GPUS", v.clamp_int(9999, 1, v.MAX_GPUS, 1) == v.MAX_GPUS)

    os.environ["MAX_HOURS"] = "72"
    importlib.reload(v)
    check("clean_time_limit clamps 999h to 72h", v.clean_time_limit("999:00:00") == "72:00:00")
    check("clean_time_limit rejects garbage", v.clean_time_limit("not-a-time") is None)
    check("clean_run_command flattens newlines", "\n" not in v.clean_run_command("a\nb"))

    import socket as _sock
    if hasattr(_sock, "AF_UNIX"):
        with tempfile.TemporaryDirectory() as sd:
            os.environ["AUDIT_SOCKET"] = str(Path(sd) / "test.sock")
            spool_dir = Path(sd) / "spool"
            os.environ["AUDIT_SPOOL"] = str(spool_dir)
            import iitgpu.auditclient as ac
            importlib.reload(ac)
            result = ac.log("selftest", detail="socket_fallback_test")
            check("audit falls back to spool when socket missing", result is True)
            spooled = list(spool_dir.iterdir()) if spool_dir.exists() else []
            check("spool file created", len(spooled) == 1)

    print(f"\n{'All checks passed!' if not failures else str(len(failures)) + ' check(s) FAILED: ' + str(failures)}\n")
    return 0 if not failures else 1


def main() -> None:
    parser = argparse.ArgumentParser(prog="iit-gpu-manager")
    parser.add_argument("--demo", action="store_true", help="Simulate SLURM (no cluster needed)")
    parser.add_argument("--no-splash", action="store_true", help="Skip ASCII splash screen")
    parser.add_argument("--selftest", action="store_true", help="Run built-in checks and exit")
    args = parser.parse_args()

    if args.selftest:
        sys.exit(_run_selftest())

    if args.demo:
        os.environ["DEMO_MODE"] = "1"

    _install_signal_handlers()

    from iitgpu import auditclient
    auditclient.log("session_start")

    import getpass as _gp
    _login_user   = _gp.getuser()
    _remote_ip    = os.environ.get("SSH_CLIENT", "").split()[0] if os.environ.get("SSH_CLIENT") else ""

    def _fire_login_notification() -> None:
        try:
            from iitgpu import daemonclient, mailer
            _email = daemonclient.email_for(_login_user)
            if _email:
                mailer.send_login_notification(_login_user, _email, _remote_ip)
        except Exception:
            pass

    import threading as _th
    _th.Thread(target=_fire_login_notification, daemon=True).start()

    try:
        if not args.no_splash:
            from iitgpu.splash import show_splash
            show_splash()
        from iitgpu.menu import run_menu
        run_menu()
    except Exception as exc:
        from iitgpu import auditclient as _ac
        _ac.log("tool_crash", detail=str(exc))
        from iitgpu.ui import err
        err(f"Unexpected error: {exc}")
        sys.exit(1)
    finally:
        from iitgpu import auditclient as _ac
        _ac.log("session_end")


if __name__ == "__main__":
    main()
