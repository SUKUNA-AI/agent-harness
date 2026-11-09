"""Explicit content-bound legacy hook adoption in isolated Codex homes."""
from copy import deepcopy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
import venv
from unittest.mock import patch

from agent_harness import installation as install
import test_install


class HookAdoptionTests(unittest.TestCase):
    setUp = test_install.InstallerTests.setUp

    def legacy(self, *, foreign=True):
        handler = {'matcher': '^(startup|resume|clear|compact|fork)$', 'hooks': [
            {'type': 'command', 'command': 'python /legacy/agent-harness-user/session.py hook',
             'timeout': 30, 'additionalContextLimit': 5000}]}
        user = {'matcher': 'startup', 'hooks': [{'type': 'command', 'command': 'printf user-handler'}]}
        groups = [user, handler] if foreign else [handler]
        path = self.home / 'hooks.json'
        path.write_text(json.dumps({'hooks': {'SessionStart': groups}}, ensure_ascii=False), encoding="utf-8")
        return handler, user, install.digest(install.canonical(handler))

    def plan(self, expected=None):
        return install.plan(self.package, self.home, self.skills, self.state, self.overlay,
                            adopt_hook_sha256=expected)

    def apply(self, expected):
        with install.lock(self.state):
            return install.apply_plan(self.plan(expected), self.state, self.package)

    def target_bytes(self):
        return {str(path.relative_to(self.home)): path.read_bytes()
                for path in self.home.rglob('*') if path.is_file()}

    def test_adoption_records_exact_previous_group_and_reinstall_is_idempotent(self):
        legacy, user, expected = self.legacy()
        config = self.home / 'config.toml'
        config.write_text(self.original_config + '\n[hooks.state.fixture]\ntrusted = true\n')
        before = self.target_bytes()
        planned = self.plan(expected)
        self.assertEqual(before, self.target_bytes(), 'adoption dry-run changed targets')
        self.assertFalse(self.state.exists())
        row = next(item for item in planned if item['path'] == self.home / 'hooks.json')
        self.assertEqual(legacy, row['meta']['handler_before'])
        self.assertEqual(expected, row['meta']['adopted_group_digest'])
        self.assertEqual(user, json.loads(row['after'])['hooks']['SessionStart'][0])
        self.apply(expected)
        installed = self.target_bytes()
        manifest = install.active_manifest(self.state)
        owned = next(item for item in manifest['files'] if item['path'] == str(self.home / 'hooks.json'))
        self.assertEqual(legacy, owned['meta']['handler_before'])
        self.assertEqual(expected, owned['meta']['adopted_group_digest'])
        self.assertEqual('unchanged', self.apply(expected)['status'])
        self.assertEqual([], self.plan())
        self.assertEqual(installed, self.target_bytes())
        self.assertIn('[hooks.state.fixture]\ntrusted = true', config.read_text())
        self.assertEqual('structural_verified', install.verify(self.package, self.home, self.skills, self.state)['status'])
        with self.assertRaisesRegex(install.InstallError, 'digest mismatch'):
            self.plan('0' * 64)
        self.assertEqual(installed, self.target_bytes())

    def test_default_wrong_digest_and_invalid_digest_refuse_before_targets(self):
        _, _, expected = self.legacy()
        before = self.target_bytes()
        for consent in (None, '0' * 64, 'wrong', expected.upper()):
            with self.subTest(consent=consent), self.assertRaises(install.InstallError):
                self.plan(consent)
            self.assertEqual(before, self.target_bytes())
            self.assertFalse(self.state.exists())
            self.assertFalse(self.skills.exists())

    def test_missing_or_multiple_candidates_and_config_duplicate_refuse(self):
        legacy, user, expected = self.legacy()
        path = self.home / 'hooks.json'
        for groups in ([user], [legacy, deepcopy(legacy)]):
            path.write_text(json.dumps({'hooks': {'SessionStart': groups}}), encoding="utf-8")
            before = self.target_bytes()
            with self.assertRaises(install.InstallError):
                self.plan(expected)
            self.assertEqual(before, self.target_bytes())
            self.assertFalse(self.state.exists())
        path.write_text(json.dumps({'hooks': {'SessionStart': [legacy]}}), encoding="utf-8")
        config = self.home / 'config.toml'
        config.write_text(self.original_config + '\n[hooks]\nSessionStart = [{command = "python session.py hook"}]\n')
        before = self.target_bytes()
        with self.assertRaisesRegex(install.InstallError, 'duplicate effective source'):
            self.plan(expected)
        self.assertEqual(before, self.target_bytes())
        self.assertFalse(self.state.exists())

    def test_mixed_group_valid_digest_refuses_without_changing_bytes_or_mode(self):
        legacy, user, _ = self.legacy()
        legacy['hooks'].append(user['hooks'][0])
        path = self.home / 'hooks.json'
        path.write_bytes(install.canonical({'hooks': {'SessionStart': [legacy]}}))
        path.chmod(0o640)
        mode = path.stat().st_mode
        before = self.target_bytes()
        expected = install.digest(install.canonical(legacy))
        with self.assertRaisesRegex(install.InstallError, 'exactly one command handler'):
            self.plan(expected)
        self.assertEqual(before, self.target_bytes())
        self.assertEqual(mode, path.stat().st_mode)
        self.assertFalse(self.state.exists())
        self.assertFalse(self.skills.exists())

    def test_default_managed_update_preserves_edited_mixed_group_as_conflict(self):
        _, user, expected = self.legacy()
        self.apply(expected)
        path = self.home / 'hooks.json'
        value = json.loads(path.read_text(encoding="utf-8"))
        value['hooks']['SessionStart'][1]['hooks'].append(user['hooks'][0])
        path.write_bytes(install.canonical(value))
        path.chmod(0o640)
        mode = path.stat().st_mode
        before = self.target_bytes()
        manifests = list((self.state / 'installs').glob('*/manifest.json'))
        with self.assertRaisesRegex(install.InstallError, 'edited harness hook conflicts'):
            self.plan()
        self.assertEqual(before, self.target_bytes())
        self.assertEqual(mode, path.stat().st_mode)
        self.assertEqual(manifests, list((self.state / 'installs').glob('*/manifest.json')))


    def test_concurrent_hook_edit_after_digest_check_preserves_new_foreign_group(self):
        _, _, expected = self.legacy()
        path = self.home / 'hooks.json'
        original = install.read
        calls = 0
        changed = json.loads(path.read_text(encoding="utf-8"))
        changed['hooks']['SessionStart'].append({'matcher': 'clear', 'hooks': [
            {'type': 'command', 'command': 'printf concurrent-user'}]})
        content = json.dumps(changed).encode()
        config_before = (self.home / 'config.toml').read_bytes()
        def editing(target):
            nonlocal calls
            if target == path:
                calls += 1
                if calls == 2:
                    path.write_bytes(content)
            return original(target)
        with patch.object(install, 'read', side_effect=editing), self.assertRaisesRegex(install.InstallError, 'Concurrent edit'):
            self.plan(expected)
        self.assertEqual(content, path.read_bytes())
        self.assertEqual(config_before, (self.home / 'config.toml').read_bytes())
        self.assertFalse(self.state.exists())


    def test_ordinary_rollback_restores_legacy_group_and_exact_original_bytes(self):
        self.legacy()
        expected = install.digest(install.canonical(json.loads((self.home / 'hooks.json').read_text(encoding="utf-8"))['hooks']['SessionStart'][1]))
        original = (self.home / 'hooks.json').read_bytes()
        self.apply(expected)
        manifest = install.active_manifest(self.state)
        before = install.read(self.state / 'active.json')
        result = install.apply_rollback(install.rollback_plan(manifest), self.state, manifest,
            json.loads(before), 'rollback', before)
        self.assertEqual('rolled_back', result['status'])
        self.assertEqual(original, (self.home / 'hooks.json').read_bytes())
        self.assertFalse((self.home / 'agent-harness-hook.py').exists())

    def test_hook_rollback_edit_under_non_utf8_default_locale(self):
        # Model the Windows3.11/3.13 default without changing process locale or
        # relying on this host's UTF-8 mode. Product JSON is explicitly UTF-8.
        original_read = Path.read_text
        def legacy_read(path, encoding=None, errors=None, **kwargs):
            return original_read(path, encoding=encoding or "cp1252", errors=errors, **kwargs)
        with patch.object(Path, "read_text", new=legacy_read):
            self.test_addressed_rollback_preserves_new_user_group_and_restores_legacy()

    def test_addressed_rollback_preserves_new_user_group_and_restores_legacy(self):
        legacy, user, expected = self.legacy()
        self.apply(expected)
        path = self.home / 'hooks.json'
        value = json.loads(path.read_text(encoding="utf-8"))
        added = {'matcher': 'clear', 'hooks': [{'type': 'command', 'command': 'printf new-user'}]}
        value['hooks']['SessionStart'].append(added)
        path.write_text(json.dumps(value), encoding="utf-8")
        manifest = install.active_manifest(self.state)
        before = install.read(self.state / 'active.json')
        install.apply_rollback(install.rollback_plan(manifest), self.state, manifest,
                               json.loads(before), 'rollback', before)
        self.assertEqual([user, legacy, added], json.loads(path.read_text(encoding="utf-8"))['hooks']['SessionStart'])

    def test_windows_command_unicode_ownership_remains_exact(self):
        command = subprocess.list2cmdline([
            r"C:\Program Files\Python313\python.exe",
            r"C:\Fixture Space\профиль .codex\agent-harness-hook.py"])
        owned = {'matcher': 'startup', 'hooks': [{'type': 'command', 'command': command}]}
        foreign = {'matcher': 'clear', 'hooks': [{'type': 'command', 'command': 'printf user-handler'}]}
        legacy = {'matcher': 'startup', 'hooks': [{'type': 'command', 'command': 'python session.py hook'}]}
        meta = {'handler_after': owned, 'handler_before': legacy}
        document = {'hooks': {'SessionStart': [foreign, owned]}}
        raw = install.canonical(document)
        self.assertEqual([foreign, legacy], install._revert_hooks(raw, meta)['hooks']['SessionStart'])
        # Locale corruption actually changes the owned command. The installer
        # must continue to refuse it rather than repairing or normalizing it.
        corrupted = json.loads(raw.decode('cp1252'))
        self.assertNotEqual(owned, corrupted['hooks']['SessionStart'][1])
        edited = deepcopy(document)
        edited['hooks']['SessionStart'][1]['hooks'][0]['command'] += ' --edited'
        duplicated = {'hooks': {'SessionStart': [foreign, owned, deepcopy(owned)]}}
        for value in (corrupted, edited, duplicated):
            with self.subTest(kind=value), self.assertRaisesRegex(install.InstallError, 'changed or duplicated'):
                install._revert_hooks(install.canonical(value), meta)
        self.assertEqual(owned, meta['handler_after'])

    def test_byte_identical_adoption_still_records_ownership(self):
        _, _, handler = install.hook_payload(self.package, self.home)
        path = self.home / 'hooks.json'
        path.write_bytes(install.canonical({'hooks': {'SessionStart': [handler]}}))
        expected = install.digest(install.canonical(handler))
        row = next(item for item in self.plan(expected) if item['path'] == path)
        self.assertEqual(row['before'], row['after'])
        self.apply(expected)
        self.assertEqual([], self.plan())
        self.assertEqual('unchanged', self.apply(expected)['status'])

    def test_real_cli_flag_dry_run_apply_repeat_and_rollback(self):
        _, _, expected = self.legacy()
        original = (self.home / 'hooks.json').read_bytes()
        tool = Path(__file__).resolve().parents[1] / 'tools/harness.py'
        base = [sys.executable, '-I', '-B', str(tool)]
        locations = ['--package-root', str(self.package), '--codex-home', str(self.home),
                     '--skills-home', str(self.skills), '--state-dir', str(self.state)]
        def run(command, *extra, code=0):
            result = subprocess.run([*base, command, *locations, *extra], capture_output=True,
                text=True, encoding='utf-8', timeout=10, env={**os.environ, 'CODEX_HOME': str(self.home)})
            self.assertEqual(code, result.returncode, result.stdout + result.stderr)
            return json.loads(result.stdout)
        self.assertEqual('dry_run', run('install', '--adopt-hook-sha256', expected)['status'])
        self.assertEqual(original, (self.home / 'hooks.json').read_bytes())
        self.assertEqual('written_pending_native_verification', run('install', '--adopt-hook-sha256', expected, '--apply')['status'])
        self.assertEqual('unchanged', run('install', '--adopt-hook-sha256', expected, '--apply')['status'])
        self.assertEqual('blocked', run('verify', '--adopt-hook-sha256', expected, code=2)['status'])
        self.assertEqual('rolled_back', run('rollback', '--apply')['status'])
        self.assertEqual(original, (self.home / 'hooks.json').read_bytes())

    @unittest.skipIf(os.name == 'nt', 'POSIX interpreter symlinks')
    def test_interpreter_alias_cli_keeps_owned_command_and_rollback(self):
        _, _, expected = self.legacy()
        original = (self.home / 'hooks.json').read_bytes()
        interpreters = self.base / 'Python aliases'
        interpreters.mkdir()
        first, second = interpreters / 'python original', interpreters / 'python alias'
        for executable in (first, second):
            executable.symlink_to(sys.executable)
        self.assertTrue(os.path.samefile(first, second))
        tool = Path(__file__).resolve().parents[1] / 'tools/harness.py'
        locations = ['--package-root', str(self.package), '--codex-home', str(self.home),
                     '--skills-home', str(self.skills), '--state-dir', str(self.state)]
        def run(executable, command, *extra):
            result = subprocess.run([str(executable), '-I', '-B', str(tool), command, *locations, *extra],
                capture_output=True, text=True, encoding='utf-8', timeout=15,
                env={**os.environ, 'CODEX_HOME': str(self.home)})
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            return json.loads(result.stdout)
        self.assertEqual('written_pending_native_verification',
                         run(first, 'install', '--adopt-hook-sha256', expected, '--apply')['status'])
        installed = self.target_bytes()
        receipt = (self.state / 'active.json').read_bytes()
        for executable in (second, first):
            self.assertEqual('structural_verified', run(executable, 'verify')['status'])
            self.assertEqual('unchanged', run(executable, 'install', '--apply')['status'])
            self.assertEqual(installed, self.target_bytes())
            self.assertEqual(receipt, (self.state / 'active.json').read_bytes())
        self.assertEqual('rolled_back', run(second, 'rollback', '--apply')['status'])
        self.assertEqual(original, (self.home / 'hooks.json').read_bytes())

    @unittest.skipIf(os.name == 'nt', 'POSIX venv interpreter symlinks')
    def test_same_binary_venv_and_directory_alias_are_not_equivalent(self):
        environment = self.base / 'separate venv'
        venv.EnvBuilder(with_pip=False, symlinks=True).create(environment)
        executable = environment / 'bin/python'
        self.assertTrue(executable.samefile(sys.executable))
        result = subprocess.run([str(executable), '-c', 'import sys; print(sys.prefix)'],
                                capture_output=True, text=True, check=True, timeout=10)
        self.assertEqual(str(environment), result.stdout.strip())
        with patch.object(sys, 'executable', str(executable)):
            self.apply(None)
            self.assertEqual('structural_verified',
                             install.verify(self.package, self.home, self.skills, self.state)['status'])
        original = self.target_bytes()
        self.assertEqual('failed', install.verify(self.package, self.home, self.skills, self.state)['status'])
        pending = self.plan()
        self.assertEqual([self.home / 'hooks.json'], [row['path'] for row in pending])
        self.assertEqual(original, self.target_bytes())
        # Even physically identical executable directories need not have the
        # same lexical ancestors for Python startup configuration discovery.
        alias = self.base / 'directory alias'
        alias.symlink_to(executable.parent, target_is_directory=True)
        self.assertTrue(alias.samefile(executable.parent))
        with patch.object(sys, 'executable', str(alias / 'python')):
            self.assertEqual('failed', install.verify(self.package, self.home, self.skills, self.state)['status'])
            self.assertEqual([self.home / 'hooks.json'], [row['path'] for row in self.plan()])
        self.assertEqual(original, self.target_bytes())

    @unittest.skipIf(os.name == 'nt', 'POSIX interpreter symlinks')
    def test_different_executable_in_same_directory_is_not_alias(self):
        directory = self.base / 'interpreters'
        directory.mkdir()
        first, different = directory / 'python alias', directory / 'different python'
        first.symlink_to(sys.executable)
        shutil.copyfile(sys.executable, different)
        self.assertFalse(first.samefile(different))
        with patch.object(sys, 'executable', str(first)):
            self.apply(None)
        original = self.target_bytes()
        with patch.object(sys, 'executable', str(different)):
            self.assertEqual('failed', install.verify(self.package, self.home, self.skills, self.state)['status'])
            self.assertEqual([self.home / 'hooks.json'], [row['path'] for row in self.plan()])
        self.assertEqual(original, self.target_bytes())

    def test_owned_hook_edits_are_conflicts_not_interpreter_aliases(self):
        self.apply(None)
        path = self.home / 'hooks.json'
        pristine = json.loads(path.read_text(encoding='utf-8'))
        for edit in ('flags', 'shell', 'launcher', 'timeout', 'matcher'):
            value = deepcopy(pristine)
            handler = value['hooks']['SessionStart'][0]
            hook = handler['hooks'][0]
            if edit == 'flags':
                hook['command'] += ' -I'
            elif edit == 'shell':
                hook['command'] += ' && echo changed'
            elif edit == 'launcher':
                hook['command'] = hook['command'].replace('agent-harness-hook.py', 'agent-harness-hook-other.py')
            elif edit == 'timeout':
                hook['timeout'] += 1
            else:
                handler['matcher'] = 'startup'
            with self.subTest(edit=edit):
                path.write_bytes(install.canonical(value))
                before = self.target_bytes()
                self.assertEqual('failed', install.verify(self.package, self.home, self.skills, self.state)['status'])
                with self.assertRaisesRegex(install.InstallError, 'edited harness hook conflicts'):
                    self.plan()
                self.assertEqual(before, self.target_bytes())

    def test_windows_hook_command_keeps_generated_quoting(self):
        argv = [r'C:\Program Files\Python313\python.exe',
                r'C:\Fixture Space\профиль .codex\agent-harness-hook.py']
        expected = subprocess.list2cmdline(argv)
        with patch.object(install.os, 'name', 'nt'):
            self.assertEqual(expected, install._hook_command(argv))
            for character in ('&', '%', '!', '\n'):
                with self.subTest(character=character), self.assertRaises(install.InstallError):
                    install._hook_command([argv[0], argv[1] + character])
