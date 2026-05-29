# iitgpu/shell.py
from __future__ import annotations

import shlex
import subprocess
import sys

from iitgpu import auditclient
from iitgpu.ui import header, info
from iitgpu.validate import in_jail

ALLOWED_COMMANDS = {"sbatch", "squeue", "scancel", "sinfo", "tail"}

# Commands that take a file path argument (sbatch: last positional; tail: last non-flag)
_PATH_ARG_COMMANDS = {"sbatch", "tail"}

_FORBIDDEN_FLAGS = {
    "sbatch": {"--wrap"},
}


def _parse_command(line: str) -> tuple[str, list[str]]:
    """Split a raw input line into (command, args). Returns ("", []) for blank."""
    parts = shlex.split(line.strip()) if line.strip() else []
    if not parts:
        return "", []
    return parts[0].lower(), parts[1:]


def _find_path_args(cmd: str, args: list[str]) -> list[str]:
    """Return all non-flag positional arguments for commands that take file paths."""
    if cmd not in _PATH_ARG_COMMANDS:
        return []
    return [a for a in args if not a.startswith("-")]


def _has_forbidden_flag(cmd: str, args: list[str]) -> bool:
    forbidden = _FORBIDDEN_FLAGS.get(cmd, set())
    for arg in args:
        flag = arg.split("=")[0]
        if flag in forbidden:
            return True
    return False


def _dispatch(cmd: str, args: list[str]) -> None:
    """Execute one allowed SLURM command. Print error and return on policy violations."""
    if cmd not in ALLOWED_COMMANDS:
        print(f"Not allowed: {cmd!r}. Allowed commands: {', '.join(sorted(ALLOWED_COMMANDS))}")
        return

    if _has_forbidden_flag(cmd, args):
        print(f"Not allowed: flag not permitted for {cmd!r}.")
        return

    for path_arg in _find_path_args(cmd, args):
        if not in_jail(path_arg):
            print(f"Access denied: {path_arg!r} is outside allowed directories.")
            return

    full_cmd = [cmd] + args
    try:
        subprocess.run(full_cmd, text=True)
    except FileNotFoundError:
        print(f"Command not found: {cmd}. Is SLURM installed?")
    except OSError as exc:
        print(f"Error running {cmd}: {exc}")


def run_shell() -> None:
    """Run the restricted SLURM command shell."""
    header("SLURM Shell  (advanced)")
    info(f"Allowed commands: {', '.join(sorted(ALLOWED_COMMANDS))}")
    info('Type "exit" to return to main menu.')
    print()

    auditclient.log("shell_start")

    while True:
        try:
            line = input("slurm> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if not line:
            continue
        if line.lower() == "exit":
            break

        auditclient.log("shell_cmd", detail=line)
        cmd, args = _parse_command(line)
        if cmd:
            _dispatch(cmd, args)

    auditclient.log("shell_exit")
    info("Returned to main menu.")
