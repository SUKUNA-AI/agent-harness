"""Deterministic converter fixture, explicitly not real Docling qualification."""
from pathlib import Path
import os
import subprocess
import sys
import time

if "--version" in sys.argv:
    print("fixture-document-converter 1")
    raise SystemExit
mode, source, target = sys.argv[1:4]
source, target = Path(source), Path(target)
if mode == "slow":
    time.sleep(30)
if mode == "crash":
    raise SystemExit(17)
if mode == "oversize":
    target.write_bytes(b"x" * 200000)
    raise SystemExit
if mode == "link":
    target.symlink_to(source)
    raise SystemExit
if mode == "child":
    child = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(30)"])
    (target.parent / "child.pid").write_text(str(child.pid))
if mode == "local":
    # Extra observable fixture evidence, independent of harness's guard.
    target.write_text("namespace=" + os.readlink("/proc/self/ns/net") + "\n" + source.read_text().upper())
else:
    target.write_text(source.read_text().upper(), encoding="utf-8")
