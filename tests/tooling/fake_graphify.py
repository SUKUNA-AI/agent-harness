"""Fixture graph builder. Never represents live Graphify qualification."""
from pathlib import Path
import json
import sys
if "--version" in sys.argv:
    print("fixture-graphify 1")
    raise SystemExit
root, output = map(Path, sys.argv[1:3])
values = {p.relative_to(root).as_posix(): p.read_text() for p in root.rglob("*.py")}
output.write_text(json.dumps(values, sort_keys=True))
