import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from agent_harness.core import _git, _git_settings


class SnapshotConfigTests(unittest.TestCase):
    def test_batched_config_preserves_missing_empty_duplicate_and_include_values(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["git", "-c", "init.templateDir=", "init", "-q", str(root)], check=True, capture_output=True)
            extra = root / "extra-config"
            extra.write_text("[core]\n eol = crlf\n", encoding="utf-8")
            config = root / ".git" / "config"
            for tail in ("", "\n[core]\n autocrlf = true\n eol = lf\n[include]\n path = " + extra.as_posix() + "\n", "\n[core]\n eol =\n fileMode = false\n"):
                with config.open("a", encoding="utf-8") as output:
                    output.write(tail)
                expected = {}
                for key in ("core.autocrlf", "core.eol", "core.filemode"):
                    value = _git(root, "config", "--get", key)
                    expected[key] = os.fsdecode(value).strip() if value is not None else None
                self.assertEqual(expected, _git_settings(root))


if __name__ == "__main__":
    unittest.main()
