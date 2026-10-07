"""Unit tests for :mod:`pmemstat.History` (the cross-run growth ledger).

Every test points ``PMEMSTAT_STATE_DIR`` at a private temporary directory, so
nothing is written to the real ``/tmp/pmemstat-<uid>`` and no root privileges
are required.
"""
import fcntl
import json
import os
import stat
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from pmemstat import History

T0 = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)


class HistoryTestCase(unittest.TestCase):
    """Base class: isolate the state directory and the tuning env vars."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._saved = {name: os.environ.get(name) for name in (
            History.STATE_DIR_ENV, History.UPDATE_SECS_ENV,
            History.GRACE_SECS_ENV)}
        os.environ[History.STATE_DIR_ENV] = self.tmp.name
        os.environ.pop(History.UPDATE_SECS_ENV, None)
        os.environ.pop(History.GRACE_SECS_ENV, None)
        self.addCleanup(self._restore_env)

    def _restore_env(self):
        for name, value in self._saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def _path(self, name):
        return os.path.join(History.state_dir(), name)

    def _write_ledger(self, text):
        with open(self._path(History.LEDGER_NAME), 'w', encoding='utf-8') as fh:
            fh.write(text)


class TestLedgerSchema(HistoryTestCase):
    """Schema round-trip and defensive handling of bad input."""

    def test_round_trip(self):
        self.assertTrue(History.update_ledger({'exe': {'foo': 123},
                                               'pid': {'1': 5}},
                                              boot='b1', now=T0))
        ledger = History.load_ledger()
        self.assertEqual(ledger['v'], History.SCHEMA_VERSION)
        self.assertEqual(ledger['boot_id'], 'b1')
        entry = ledger['views']['exe']['foo']
        self.assertEqual(entry, {'base': 123, 'base_ts': T0.isoformat(),
                                 'last': 123, 'last_ts': T0.isoformat()})
        for field in ('updated_ts', 'boot_id', 'host', 'uid', 'tool'):
            self.assertIn(field, ledger)

    def test_missing_file_is_none(self):
        self.assertIsNone(History.load_ledger())

    def test_corrupt_file_treated_as_empty(self):
        self._write_ledger('{ this is not json')
        self.assertIsNone(History.load_ledger())
        # A subsequent update starts from scratch rather than crashing.
        self.assertTrue(History.update_ledger({'exe': {'a': 1}}, boot='b'))
        self.assertEqual(History.load_ledger()['views']['exe']['a']['base'], 1)

    def test_unknown_version_treated_as_empty(self):
        self._write_ledger(json.dumps({'v': 999, 'views': {'exe': {}}}))
        self.assertIsNone(History.load_ledger())
        self.assertEqual(History.baselines(boot='b'), {})

    def test_prior_schema_version_resets_ledger(self):
        # An older schema (e.g. v2) is discarded, never reinterpreted: the next
        # write starts a fresh ledger at the current version.
        self._write_ledger(json.dumps({'v': History.SCHEMA_VERSION - 1,
                                       'views': {'exe': {'a': {'base': 111}}}}))
        self.assertIsNone(History.load_ledger())
        self.assertEqual(History.baselines(boot='b'), {})
        History.update_ledger({'exe': {'a': 5}}, boot='b')
        ledger = History.load_ledger()
        self.assertEqual(ledger['v'], History.SCHEMA_VERSION)
        self.assertEqual(ledger['views']['exe']['a']['base'], 5)

    def test_non_dict_views_treated_as_empty(self):
        self._write_ledger(json.dumps({'v': History.SCHEMA_VERSION,
                                       'views': [1, 2, 3]}))
        self.assertIsNone(History.load_ledger())

    def test_baselines_shape(self):
        History.update_ledger({'exe': {'a': 42}, 'pid': {'1': 5}},
                              boot='b', now=T0)
        base = History.baselines(boot='b')
        self.assertEqual(base['exe']['a'],
                         {'base': 42, 'base_ts': T0.isoformat()})
        self.assertEqual(base['pid']['1']['base'], 5)

    def test_sys_round_trip_and_baselines(self):
        History.update_ledger({'exe': {'a': 1}},
                              sys_samples={'Used': 500, 'OthK': 10},
                              boot='b', now=T0)
        ledger = History.load_ledger()
        self.assertEqual(ledger['sys']['Used']['base'], 500)
        self.assertEqual(History.sys_baselines(boot='b')['Used'],
                         {'base': 500, 'base_ts': T0.isoformat()})
        # A different boot yields no system baselines (boot reset).
        self.assertEqual(History.sys_baselines(boot='other'), {})

    def test_sys_key_gc_respects_grace(self):
        History.update_ledger({}, sys_samples={'Used': 100, 'OthK': 5},
                              boot='b', now=T0)
        within = T0 + timedelta(seconds=History.GRACE_SECS - 1)
        History.update_ledger({}, sys_samples={'Used': 101}, boot='b',
                              now=within)
        self.assertEqual(set(History.load_ledger()['sys']), {'Used', 'OthK'})
        beyond = T0 + timedelta(seconds=History.GRACE_SECS + 1)
        History.update_ledger({}, sys_samples={'Used': 102}, boot='b',
                              now=beyond)
        self.assertEqual(set(History.load_ledger()['sys']), {'Used'})


class TestMergeSemantics(HistoryTestCase):
    """Add/update, boot reset and grace-based garbage collection."""

    def test_base_fixed_last_refreshed(self):
        History.update_ledger({'exe': {'a': 100}}, boot='b', now=T0)
        t1 = T0 + timedelta(seconds=60)
        History.update_ledger({'exe': {'a': 150}}, boot='b', now=t1)
        entry = History.load_ledger()['views']['exe']['a']
        self.assertEqual(entry['base'], 100)
        self.assertEqual(entry['base_ts'], T0.isoformat())
        self.assertEqual(entry['last'], 150)
        self.assertEqual(entry['last_ts'], t1.isoformat())

    def test_boot_change_resets_the_ledger(self):
        History.update_ledger({'exe': {'a': 100}}, boot='boot-1')
        History.update_ledger({'exe': {'b': 5}}, boot='boot-2')
        ledger = History.load_ledger()
        self.assertEqual(ledger['boot_id'], 'boot-2')
        self.assertEqual(set(ledger['views']['exe']), {'b'})
        self.assertEqual(History.baselines(boot='boot-1'), {})

    def test_absent_key_kept_within_grace(self):
        History.update_ledger({'exe': {'a': 10, 'b': 20}}, boot='b', now=T0)
        within = T0 + timedelta(seconds=History.GRACE_SECS - 1)
        History.update_ledger({'exe': {'a': 11}}, boot='b', now=within)
        keys = set(History.load_ledger()['views']['exe'])
        self.assertEqual(keys, {'a', 'b'})
        self.assertEqual(History.load_ledger()['views']['exe']['b']['last'], 20)

    def test_absent_key_removed_after_grace(self):
        History.update_ledger({'exe': {'a': 10, 'b': 20}}, boot='b', now=T0)
        beyond = T0 + timedelta(seconds=History.GRACE_SECS + 1)
        History.update_ledger({'exe': {'a': 12}}, boot='b', now=beyond)
        keys = set(History.load_ledger()['views']['exe'])
        self.assertEqual(keys, {'a'})

    def test_empty_view_is_dropped(self):
        History.update_ledger({'exe': {'a': 10}}, boot='b', now=T0)
        beyond = T0 + timedelta(seconds=History.GRACE_SECS + 1)
        History.update_ledger({'pid': {'9': 1}}, boot='b', now=beyond)
        views = History.load_ledger()['views']
        self.assertNotIn('exe', views)
        self.assertIn('pid', views)


class TestLocking(HistoryTestCase):
    """A held lock makes ``update_ledger`` skip rather than block."""

    def _hold_lock(self):
        path = self._path(History.LOCK_NAME)
        fd = os.open(path, os.O_RDWR | os.O_CREAT, History.FILE_MODE)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd

    def test_contention_returns_false_and_writes_nothing(self):
        self._hold_lock()
        self.assertFalse(History.update_ledger({'exe': {'a': 1}}, boot='b'))
        self.assertIsNone(History.load_ledger())

    def test_update_succeeds_once_lock_released(self):
        fd = self._hold_lock()
        self.assertFalse(History.update_ledger({'exe': {'a': 1}}, boot='b'))
        fcntl.flock(fd, fcntl.LOCK_UN)
        self.assertTrue(History.update_ledger({'exe': {'a': 1}}, boot='b'))
        self.assertEqual(History.load_ledger()['views']['exe']['a']['base'], 1)


class TestConfig(HistoryTestCase):
    """Env-overridable cadence/grace configuration."""

    def test_update_secs_default_and_override(self):
        self.assertEqual(History.update_secs(), History.DEFAULT_UPDATE_SECS)
        with mock.patch.dict(os.environ, {History.UPDATE_SECS_ENV: '42'}):
            self.assertEqual(History.update_secs(), 42.0)

    def test_gc_grace_default_and_override(self):
        self.assertEqual(History.gc_grace_secs(), History.GRACE_SECS)
        with mock.patch.dict(os.environ, {History.GRACE_SECS_ENV: '5'}):
            self.assertEqual(History.gc_grace_secs(), 5.0)


class TestCleanupObsolete(HistoryTestCase):
    """Startup deletes the phase-1 anchor/live/marker files."""

    def test_legacy_files_removed_ledger_kept(self):
        for name in History.LEGACY_NAMES:
            with open(self._path(name), 'w', encoding='utf-8') as fh:
                fh.write('stale')
        History.update_ledger({'exe': {'a': 1}}, boot='b')
        History.cleanup_obsolete()
        for name in History.LEGACY_NAMES:
            self.assertFalse(os.path.exists(self._path(name)), name)
        self.assertIsNotNone(History.load_ledger())

    def test_missing_files_are_not_an_error(self):
        History.cleanup_obsolete()  # nothing to delete


class TestOwnershipAndStateDir(HistoryTestCase):
    """Owning-uid resolution and the private, per-uid default directory."""

    def test_owning_uid_uses_sudo_when_root(self):
        with mock.patch.object(History.os, 'geteuid', return_value=0), \
             mock.patch.dict(os.environ,
                             {'SUDO_UID': '4321', 'SUDO_GID': '4322'}):
            self.assertEqual(History.owning_uid(), 4321)
            self.assertEqual(History.owning_gid(), 4322)

    def test_owning_uid_ignores_sudo_when_not_root(self):
        with mock.patch.object(History.os, 'geteuid', return_value=1000), \
             mock.patch.dict(os.environ, {'SUDO_UID': '4321'}):
            self.assertEqual(History.owning_uid(), 1000)

    def test_override_state_dir_is_used(self):
        self.assertEqual(History.state_dir(), self.tmp.name)

    def test_default_state_dir_is_private_and_per_uid(self):
        with mock.patch.object(History.os, 'geteuid', return_value=1000), \
             mock.patch.object(History.tempfile, 'gettempdir',
                               return_value=self.tmp.name), \
             mock.patch.dict(os.environ, {History.STATE_DIR_ENV: ''}):
            path = History.state_dir()
            self.assertEqual(path, os.path.join(self.tmp.name, 'pmemstat-1000'))
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode),
                             History.DIR_MODE)


class TestResetLedger(HistoryTestCase):
    """``reset_ledger`` replaces baselines so all growth starts from "now"."""

    def test_reset_discards_previous_baselines(self):
        History.update_ledger({'exe': {'a': 100, 'b': 7}}, boot='b', now=T0)
        later = T0 + timedelta(seconds=3600)
        self.assertTrue(History.reset_ledger({'exe': {'a': 250}},
                                             boot='b', now=later))
        ledger = History.load_ledger()
        entry = ledger['views']['exe']['a']
        # The old base (100) is gone; the value now seen becomes the base.
        self.assertEqual(entry['base'], 250)
        self.assertEqual(entry['last'], 250)
        self.assertEqual(entry['base_ts'], later.isoformat())
        self.assertEqual(entry['last_ts'], later.isoformat())
        # A key absent from the reset samples is dropped (no grace carry-over).
        self.assertNotIn('b', ledger['views']['exe'])

    def test_reset_refreshes_sys_baselines(self):
        History.update_ledger({'exe': {'a': 1}}, sys_samples={'Used': 500},
                              boot='b', now=T0)
        later = T0 + timedelta(seconds=60)
        History.reset_ledger({'exe': {'a': 1}}, sys_samples={'Used': 900},
                             boot='b', now=later)
        base = History.sys_baselines(boot='b')
        self.assertEqual(base['Used']['base'], 900)
        self.assertEqual(base['Used']['base_ts'], later.isoformat())

    def test_reset_replaces_a_ledger_from_another_boot(self):
        History.update_ledger({'exe': {'a': 5}}, boot='old', now=T0)
        self.assertTrue(History.reset_ledger({'exe': {'a': 42}}, boot='b',
                                             now=T0))
        self.assertEqual(History.baselines(boot='b')['exe']['a']['base'], 42)
        self.assertEqual(History.baselines(boot='old'), {})

    def test_reset_contention_returns_false_and_writes_nothing(self):
        path = self._path(History.LOCK_NAME)
        fd = os.open(path, os.O_RDWR | os.O_CREAT, History.FILE_MODE)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.assertFalse(History.reset_ledger({'exe': {'a': 1}}, boot='b'))
        self.assertIsNone(History.load_ledger())


if __name__ == '__main__':
    unittest.main()
