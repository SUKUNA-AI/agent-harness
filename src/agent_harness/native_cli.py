"""Inspect or explicitly build the local Rust helper; no background services."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys

from .core import HarnessError


def dispatch(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status", help="Show selected backend and verified bundled binary")
    build = commands.add_parser("build", help="Explicit source build; requires Cargo and a C toolchain")
    build.add_argument("--offline", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command == "build":
            script = Path(__file__).resolve().parents[2] / "tools" / "build_native.py"
            if not script.is_file():
                raise HarnessError("Native builds require the full source checkout")
            return subprocess.call([sys.executable, str(script), *(["--offline"] if args.offline else [])])
        from .native import backend_info
        print(json.dumps(backend_info(), ensure_ascii=False, indent=2))
        return 0
    except (HarnessError, OSError, ValueError) as error:
        print(json.dumps({"error": str(error), "type": type(error).__name__}, ensure_ascii=False), file=sys.stderr)
        return 2
