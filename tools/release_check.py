"""Offline release checks: packaging, private-file exclusions and skill metadata.

This is a bounded secret-pattern scan, not a guarantee against every secret.
"""
from pathlib import Path
import json
import re
import sys
import tomllib

ROOT = Path(__file__).resolve().parents[1]
IGNORED = {".git", "__pycache__", ".test-tmp", ".venv", "build", "dist"}
DENIED = {"auth.json", "machine.json", "harness-machine.json", ".env"}
PATTERNS = {
    "github_token": r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})\b",
    "openai_token": r"\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{32,}\b",
    "private_key": r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----",
    "credential_url": r"https?://[^\s/@:]+:[^\s/@]+@",
    "local_machine_path": r"(?:[CE]:[/\\](?:Users|Projects|Codex)[/\\])",
}


def skill_errors(path):
    text = path.read_text(encoding="utf-8-sig")
    errors = []
    match = re.match(r"\A---\r?\n(.*?)\r?\n---(?:\r?\n|\Z)", text, re.S)
    if not match:
        return [f"{path.relative_to(ROOT)}: missing YAML frontmatter"]
    # Intentionally a small supported subset, not an implementation of YAML.
    header = match[1]
    fields = {}
    for line in header.splitlines():
        row = re.fullmatch(r"(name|description):\s*(.+)", line)
        if row:
            if row[1] in fields:
                errors.append(f"{path.parent.name}: duplicate {row[1]}")
            fields[row[1]] = row[2].strip().strip('"\'')
    name = fields.get("name", "")
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name) or len(name) > 64:
        errors.append(f"{path.parent.name}: invalid name")
    if name != path.parent.name:
        errors.append(f"{path.parent.name}: folder/name mismatch")
    if not fields.get("description"):
        errors.append(f"{path.parent.name}: description missing")
    for marker in ("TODO", "[TODO", "TBD"):
        if marker in text:
            errors.append(f"{path.parent.name}: unfinished scaffold marker {marker}")
    return errors


def main():
    errors = []
    files = [p for p in ROOT.rglob("*") if p.is_file() and not any(part in IGNORED or part.endswith(".egg-info") for part in p.relative_to(ROOT).parts)]
    for path in files:
        rel = path.relative_to(ROOT).as_posix()
        if path.is_symlink() or path.name in DENIED or rel.startswith(("private/", "runs/", "backups/", "adapters/projects/")):
            errors.append(f"{rel}: private or nonportable file")
        try:
            text = path.read_text(encoding="utf-8-sig")
        except UnicodeDecodeError:
            errors.append(f"{rel}: unreviewed binary")
            continue
        for name, pattern in PATTERNS.items():
            if re.search(pattern, text):
                errors.append(f"{rel}: {name}")
        if path.suffix == ".json":
            try:
                json.loads(text)
            except ValueError:
                errors.append(f"{rel}: invalid JSON")
        if path.suffix == ".toml":
            try:
                tomllib.loads(text)
            except ValueError:
                errors.append(f"{rel}: invalid TOML")
        if path.suffix == ".md":
            for target in re.findall(r"\[[^\]]*\]\(([^)]+)\)", text):
                if re.match(r"https?://|mailto:|#", target):
                    continue
                dest = (path.parent / target.split("#")[0]).resolve()
                if not dest.is_relative_to(ROOT) or not dest.exists():
                    errors.append(f"{rel}: broken/outside link {target}")
    for path in sorted((ROOT / "skills").glob("*/SKILL.md")):
        errors.extend(skill_errors(path))
    required = ["skills/harness-skill-creator/SKILL.md", "skills/harness-token-economy/SKILL.md", "skills/harness-integrations/SKILL.md", "integrations/README.md", "adapters/codex/SKILL.md", "policies/global.md"]
    errors.extend(f"missing {name}" for name in required if not (ROOT / name).is_file())
    sys.path.insert(0, str(ROOT / "src"))
    from agent_harness import __version__
    from agent_harness.installation import VERSION
    project_version = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    if len({__version__, VERSION, project_version}) != 1:
        errors.append("package/installer/project version mismatch")
    print(json.dumps({"status": "failed" if errors else "passed", "files_checked": len(files), "skills_checked": len(list((ROOT / "skills").glob("*/SKILL.md"))), "errors": errors}, ensure_ascii=False, indent=2))
    return int(bool(errors))


if __name__ == "__main__":
    raise SystemExit(main())
