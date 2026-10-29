"""Unit tests for :mod:`pmemstat.UiPrefs` (the durable UI preference store).

These are pure-logic tests: no curses, no /proc.  Every test points the store at
a temporary path, so nothing here can touch a real user's config directory.
"""
import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from pmemstat import UiPrefs as module

TMP = tempfile.mkdtemp(prefix='pmemstat-ui-')


class TestConfigDir(unittest.TestCase):
    """Where the preference file lives (and why)."""

    def test_env_override_wins(self):
        target = os.path.join(TMP, 'override')
        with mock.patch.dict(os.environ, {module.CONFIG_DIR_ENV: target}):
            self.assertEqual(module.config_dir(), target)
            self.assertTrue(os.path.isdir(target))

    def test_sudo_user_home_is_used(self):
        """A root run must remember the invoking user's choice, not root's."""
        record = SimpleNamespace(pw_dir='/home/alice', pw_uid=1000, pw_gid=1000)
        env = {'SUDO_USER': 'alice'}
        env.pop(module.CONFIG_DIR_ENV, None)
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(module.pwd, 'getpwnam', return_value=record), \
                mock.patch.object(module.os, 'makedirs'), \
                mock.patch.object(module, '_chown_to_real_user'):
            self.assertEqual(module.config_dir(create=False),
                             '/home/alice/.config/pmemstat')

    def test_xdg_config_home_is_honoured(self):
        env = {'XDG_CONFIG_HOME': '/xdg'}
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(module.os, 'makedirs'), \
                mock.patch.object(module, '_chown_to_real_user'):
            self.assertEqual(module.config_dir(create=False),
                             '/xdg/pmemstat')


class TestUiPrefs(unittest.TestCase):
    """Loading, mutation and atomic saving."""

    def _prefs(self, name='ui.json'):
        return module.UiPrefs(os.path.join(TMP, name))

    def test_defaults_when_the_file_is_missing(self):
        prefs = self._prefs('missing.json')
        self.assertEqual(prefs.get('theme'), '')
        self.assertTrue(prefs.get('colors'))
        self.assertFalse(prefs.dirty)

    def test_round_trip(self):
        path = os.path.join(TMP, 'round.json')
        prefs = module.UiPrefs(path)
        prefs.set('theme', 'nord')
        prefs.set('colors', False)
        self.assertTrue(prefs.save())
        self.assertFalse(prefs.dirty)
        reloaded = module.UiPrefs(path)
        self.assertEqual(reloaded.get('theme'), 'nord')
        self.assertFalse(reloaded.get('colors'))

    def test_saved_file_is_private(self):
        path = os.path.join(TMP, 'private.json')
        prefs = module.UiPrefs(path)
        prefs.set('theme', 'gruvbox')
        prefs.save()
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)

    def test_corrupt_file_keeps_defaults(self):
        path = os.path.join(TMP, 'corrupt.json')
        with open(path, 'w', encoding='utf-8') as handle:
            handle.write('{ this is not json')
        prefs = module.UiPrefs(path)
        self.assertEqual(prefs.get('theme'), '')
        self.assertTrue(prefs.get('colors'))

    def test_non_dict_json_keeps_defaults(self):
        path = os.path.join(TMP, 'list.json')
        with open(path, 'w', encoding='utf-8') as handle:
            json.dump(['nope'], handle)
        self.assertEqual(module.UiPrefs(path).get('theme'), '')

    def test_wrong_types_are_ignored(self):
        """A hand-edited "colors": 1 must not pass as a bool."""
        path = os.path.join(TMP, 'types.json')
        with open(path, 'w', encoding='utf-8') as handle:
            json.dump({'theme': 5, 'colors': 1}, handle)
        prefs = module.UiPrefs(path)
        self.assertEqual(prefs.get('theme'), '')
        self.assertTrue(prefs.get('colors'))

    def test_unknown_keys_are_dropped_on_save(self):
        path = os.path.join(TMP, 'obsolete.json')
        with open(path, 'w', encoding='utf-8') as handle:
            json.dump({'theme': 'nord', 'obsolete': 42}, handle)
        prefs = module.UiPrefs(path)
        self.assertTrue(prefs.dirty)
        prefs.save()
        with open(path, encoding='utf-8') as handle:
            saved = json.load(handle)
        self.assertEqual(saved, {'theme': 'nord', 'colors': True})

    def test_set_validates_key_and_type(self):
        prefs = self._prefs('valid.json')
        with self.assertRaises(KeyError):
            prefs.set('nope', 1)
        with self.assertRaises(TypeError):
            prefs.set('colors', 'yes')
        # An unchanged value does not dirty the store.
        prefs.set('colors', True)
        self.assertFalse(prefs.dirty)

    def test_save_is_a_no_op_when_clean(self):
        path = os.path.join(TMP, 'clean.json')
        prefs = module.UiPrefs(path)
        self.assertTrue(prefs.save())
        self.assertFalse(os.path.exists(path))

    def test_save_failure_is_reported_not_raised(self):
        prefs = self._prefs('fail.json')
        prefs.set('theme', 'nord')
        with mock.patch.object(module.os, 'replace', side_effect=OSError('nope')):
            self.assertFalse(prefs.save())
        self.assertTrue(prefs.dirty)  # still pending, so a later save can retry


class TestThemePrefs(unittest.TestCase):
    """The console-window ``ThemeStore`` adapter."""

    def test_load_and_save_round_trip(self):
        prefs = module.UiPrefs(os.path.join(TMP, 'theme.json'))
        store = module.ThemePrefs(prefs)
        self.assertIsNone(store.load())          # nothing remembered
        store.save('nord')
        self.assertEqual(store.load(), 'nord')
        store.save('')
        self.assertIsNone(store.load())

    def test_saving_persists_immediately(self):
        path = os.path.join(TMP, 'theme-persist.json')
        store = module.ThemePrefs(module.UiPrefs(path))
        store.save('solarized-dark')
        self.assertEqual(module.UiPrefs(path).get('theme'), 'solarized-dark')


if __name__ == '__main__':
    unittest.main()
