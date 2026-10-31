from __future__ import annotations

import json
from pathlib import Path
import re
import shutil
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins" / "harness-assistant"


class PluginTests(unittest.TestCase):
    def test_portable_and_compatibility_metadata_agree(self):
        portable = json.loads((PLUGIN / "plugin.json").read_text(encoding="utf-8"))
        compatibility = json.loads((PLUGIN / ".codex-plugin/plugin.json").read_text(encoding="utf-8"))
        self.assertEqual("https://agent-plugins.org/schemas/1.0.0/plugin.schema.json", portable["$schema"])
        for key in ("name", "version", "description"):
            self.assertEqual(portable[key], compatibility[key])
        self.assertEqual("harness-assistant", portable["name"])
        self.assertEqual("./skills/", compatibility["skills"])
        self.assertEqual({}, portable["extensions"]["com.openai"]["hooks"])
        self.assertEqual({}, compatibility["hooks"])
        self.assertEqual(portable["extensions"]["com.openai"]["interface"], compatibility["interface"])

    def test_marketplace_resolves_from_repository_root(self):
        marketplace = json.loads((ROOT / ".agents/plugins/marketplace.json").read_text(encoding="utf-8"))
        self.assertEqual("harness-local", marketplace["name"])
        self.assertEqual(1, len(marketplace["plugins"]))
        entry = marketplace["plugins"][0]
        self.assertEqual({"installation": "AVAILABLE", "authentication": "ON_INSTALL"}, entry["policy"])
        self.assertEqual("local", entry["source"]["source"])
        self.assertTrue(entry["source"]["path"].startswith("./"))
        self.assertEqual(PLUGIN.resolve(), (ROOT / entry["source"]["path"]).resolve())

    def test_plugin_is_self_contained_after_standalone_copy(self):
        with tempfile.TemporaryDirectory(prefix="harness-plugin-") as directory:
            copy = Path(directory) / "standalone"
            shutil.copytree(PLUGIN, copy)
            self.assertEqual({"harness-coordinate", "harness-run-status"}, {p.name for p in (copy / "skills").iterdir()})
            for path in copy.rglob("*"):
                self.assertFalse(path.is_symlink())
                if not path.is_file():
                    continue
                text = path.read_text(encoding="utf-8")
                self.assertNotIn("{{", text)
                self.assertNotIn("CODEX_PLUGIN_ROOT", text)
                self.assertNotRegex(text, r"(?<![A-Za-z])[A-Za-z]:[/\\]")
                for relative in re.findall(r'"(\./[^"\n]+)"', text):
                    target = (copy / relative).resolve()
                    self.assertTrue(target.is_relative_to(copy.resolve()))
                    self.assertTrue(target.exists(), relative)
                for target in re.findall(r"\[[^\]]+\]\(([^)]+)\)", text):
                    if not re.match(r"https?://|#", target):
                        linked = (path.parent / target.split("#")[0]).resolve()
                        self.assertTrue(linked.is_relative_to(copy.resolve()))
                        self.assertTrue(linked.exists())
            for forbidden in ("mcp.json", ".mcp.json", ".app.json", "hooks", "scripts", "AGENTS.md"):
                self.assertFalse((copy / forbidden).exists(), forbidden)

    def test_two_skills_have_valid_discovery_metadata_and_boundaries(self):
        for path in (PLUGIN / "skills").glob("*/SKILL.md"):
            text = path.read_text(encoding="utf-8")
            match = re.match(r"\A---\nname: ([a-z0-9-]+)\ndescription: ([^\n]+)\n---\n", text)
            self.assertIsNotNone(match, path)
            self.assertEqual(path.parent.name, match[1])
            self.assertGreater(len(match[2]), 30)
        status = (PLUGIN / "skills/harness-run-status/SKILL.md").read_text(encoding="utf-8")
        self.assertIn("evidence_freshness=not_checked", status)
        self.assertIn("run_status", status)
        self.assertIn("status --run-dir", status)
        coordination = (PLUGIN / "skills/harness-coordinate/SKILL.md").read_text(encoding="utf-8")
        self.assertIn("2–3", coordination)
        self.assertIn("model/provider/permissions", coordination)
        self.assertIn("ready_for_review", coordination)


if __name__ == "__main__":
    unittest.main()
