# iitgpu/daemoncli.py
"""CLI wrapper around daemon_request for use from shell scripts.

Usage: python3 -m iitgpu.daemoncli <verb> [--key value ...]

Example:
    python3 -m iitgpu.daemoncli users.offboard --username alice
    python3 -m iitgpu.daemoncli users.email_for --username alice
"""
from __future__ import annotations

import json
import sys

from iitgpu.auditclient import daemon_request


def main() -> None:
    args = sys.argv[1:]
    if not args:
        print("Usage: python3 -m iitgpu.daemoncli <verb> [--key value ...]",
              file=sys.stderr)
        sys.exit(1)
    verb    = args[0]
    payload: dict = {}
    i = 1
    while i < len(args):
        if args[i].startswith("--") and i + 1 < len(args):
            payload[args[i][2:]] = args[i + 1]
            i += 2
        else:
            i += 1
    resp = daemon_request(verb, payload)
    if resp.get("ok"):
        data = resp.get("data") or {}
        print(json.dumps(data))
    else:
        print(f"Error: {resp.get('error', 'unknown error')}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
