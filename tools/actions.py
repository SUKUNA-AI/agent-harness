"""Generic app shortcuts; no LLM, publishing or project-specific commands."""
from pathlib import Path
import argparse
import json
import os
import subprocess
import sys

PACKAGE = Path(__file__).resolve().parents[1]
ENTRY = PACKAGE / "tools/harness.py"


def execute(args):
    return subprocess.run([sys.executable, str(ENTRY), *args]).returncode


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["doctor", "quick", "acceptance", "status"])
    parser.add_argument("--run-dir", type=Path)
    args = parser.parse_args()
    if args.action == "quick":
        return subprocess.run([sys.executable, str(PACKAGE / "tools/run_tests.py")]).returncode
    if args.action == "doctor":
        return execute(["doctor"])
    if args.run_dir:
        return execute(["gate" if args.action == "acceptance" else "status", "--run-dir", str(args.run_dir)])
    home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    try:
        pointer = json.loads((home / "harness-machine.json").read_text(encoding="utf-8"))
        machine = json.loads(Path(pointer["machine_overlay"]).read_text(encoding="utf-8"))
    except (OSError, ValueError, KeyError) as error:
        print(f"Установка harness не найдена или некорректна: {type(error).__name__}. Укажите --run-dir.")
        return 2
    code = execute(["runs", "--store", machine["run_store"]])
    if args.action == "acceptance":
        print("Укажите --run-dir из списка. Произвольный последний run автоматически не выбирается.")
        return code or 2
    return code


if __name__ == "__main__":
    raise SystemExit(main())
