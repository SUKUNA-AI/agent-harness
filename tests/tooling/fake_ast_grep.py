"""Deterministic CLI protocol fixture; not live ast-grep qualification."""
import json
from pathlib import Path
import re
import sys

args = sys.argv[1:]
if "--version" in args:
    print("ast-grep 0.0.0-harness-fixture")
    raise SystemExit
if "--help" in args:
    print("run --pattern --rewrite --update-all --json --lang")
    raise SystemExit
pattern = args[args.index("--pattern") + 1]
regex = re.compile(re.escape(pattern).replace(re.escape("$A"), r"([^()]*)"))
rewrite = args[args.index("--rewrite") + 1] if "--rewrite" in args else None
paths = [Path(arg) for arg in args if arg.endswith(".py")]
matches = []
for path in paths:
    text = path.read_text()
    matches.extend({"file": str(path), "text": match.group()} for match in regex.finditer(text))
    if rewrite is not None:
        value = regex.sub(lambda match: rewrite.replace("$A", match[1]), text)
        path.write_text(value)
if rewrite is None:
    print(json.dumps(matches))
    raise SystemExit(0 if matches else 1)
