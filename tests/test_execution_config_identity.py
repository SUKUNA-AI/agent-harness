"""Native per-invocation model/provider config participates in exact resume."""
from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from agent_harness.execution import _configuration_identity


class ExecutionConfigurationIdentityTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.root = self.base / "project"
        self.root.mkdir()
        self.home = self.base / "home"
        self.home.mkdir()

    def test_explicit_config_bytes_drift_without_tool_fragment_changes(self):
        config = self.base / "model.json"
        config.write_text('{"model":"openrouter/deepseek/model-a"}')
        adapter = SimpleNamespace(environment={"HOME": str(self.home), "OPENCODE_CONFIG": str(config)})
        before = _configuration_identity(adapter, self.root)
        config.write_text('{"model":"openrouter/deepseek/model-b"}')
        self.assertNotEqual(before, _configuration_identity(adapter, self.root))

    def test_relative_config_directory_is_bound_to_the_worktree(self):
        directory = self.root / "config"
        directory.mkdir()
        config = directory / "opencode.jsonc"
        config.write_text('{"providers":{}}')
        adapter = SimpleNamespace(environment={"HOME": str(self.home), "OPENCODE_CONFIG_DIR": "config"})
        before = _configuration_identity(adapter, self.root)
        config.write_text('{"providers":{"changed":{}}}')
        self.assertNotEqual(before, _configuration_identity(adapter, self.root))

    def test_adapter_home_is_used_for_native_user_config(self):
        directory = self.home / ".config" / "opencode"
        directory.mkdir(parents=True)
        config = directory / "opencode.json"
        config.write_text('{"providers":{}}')
        adapter = SimpleNamespace(environment={"HOME": str(self.home)})
        before = _configuration_identity(adapter, self.root)
        config.write_text('{"model":"openrouter/anthropic/claude-opus-5.5"}')
        self.assertNotEqual(before, _configuration_identity(adapter, self.root))

    def test_explicit_tilde_path_uses_adapter_home(self):
        config = self.home / "model.json"
        config.write_text('{"model":"first"}')
        adapter = SimpleNamespace(environment={"HOME": str(self.home), "OPENCODE_CONFIG": "~/model.json"})
        before = _configuration_identity(adapter, self.root)
        config.write_text('{"model":"second"}')
        self.assertNotEqual(before, _configuration_identity(adapter, self.root))


if __name__ == "__main__":
    unittest.main()
