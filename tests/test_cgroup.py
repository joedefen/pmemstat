#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tests for the cgroup v2 helpers in pmemstat.CGroup.

These tests never touch the real ``/proc`` or ``/sys/fs/cgroup``: the
parsers take text and the CGroup reader's ``root`` is redirected to a
temporary directory.
"""

import os
import tempfile
import unittest

from pmemstat.CGroup import (CGroup, cgroup_leaf, decode_cgroup_name,
                             has_descendant, local_values, parse_cgroup_lines,
                             parse_memory_current, parse_memory_pressure,
                             parse_memory_stat, path_is_descendant)


class TestParseCgroupLines(unittest.TestCase):
    """The /proc/<pid>/cgroup parser."""

    def test_unified_v2(self):
        self.assertEqual(parse_cgroup_lines(['0::/system.slice/foo.service']),
                         '/system.slice/foo.service')

    def test_hybrid_picks_unified_line(self):
        lines = ['1:name=systemd:/user.slice/user-1000.slice/session-2.scope',
                 '0::/user.slice/user-1000.slice/session-2.scope']
        self.assertEqual(parse_cgroup_lines(lines),
                         '/user.slice/user-1000.slice/session-2.scope')

    def test_root(self):
        self.assertEqual(parse_cgroup_lines(['0::/']), '/')

    def test_empty_path_is_root(self):
        self.assertEqual(parse_cgroup_lines(['0::']), '/')

    def test_v1_only_returns_none(self):
        lines = ['11:cpuset:/', '10:memory:/foo', '1:name=systemd:/bar']
        self.assertIsNone(parse_cgroup_lines(lines))

    def test_malformed_lines_ignored(self):
        self.assertIsNone(parse_cgroup_lines(['nonsense', '0:']))

    def test_no_lines(self):
        self.assertIsNone(parse_cgroup_lines([]))


class TestCgroupLeaf(unittest.TestCase):
    """The short-label helper."""

    def test_leaf(self):
        self.assertEqual(cgroup_leaf('/system.slice/foo.service'), 'foo.service')

    def test_trailing_slash(self):
        self.assertEqual(cgroup_leaf('/user.slice/user-1000.slice/'),
                         'user-1000.slice')

    def test_single_component(self):
        self.assertEqual(cgroup_leaf('/app'), 'app')

    def test_root_and_empty(self):
        self.assertEqual(cgroup_leaf('/'), '(root)')
        self.assertEqual(cgroup_leaf(''), '(root)')
        self.assertEqual(cgroup_leaf(None), '(root)')

    def test_escaped_hyphen_decoded(self):
        self.assertEqual(cgroup_leaf('/user.slice/app-foo\\x2dbar.service'),
                         'app-foo-bar.service')

    def test_decode_cgroup_name(self):
        self.assertEqual(decode_cgroup_name('a\\x2db\\x2dc'), 'a-b-c')
        self.assertEqual(decode_cgroup_name('plain'), 'plain')
        self.assertEqual(decode_cgroup_name(''), '')


class TestMemoryParsers(unittest.TestCase):
    """The memory.current/stat/pressure parsers."""

    def test_current(self):
        self.assertEqual(parse_memory_current('123456\n'), 123456)

    def test_current_bad(self):
        self.assertIsNone(parse_memory_current('garbage'))
        self.assertIsNone(parse_memory_current(None))

    def test_stat(self):
        text = 'anon 4096\nfile 8192\nslab 1024\nbadline\nk 12 34\n'
        self.assertEqual(parse_memory_stat(text.splitlines()),
                         {'anon': 4096, 'file': 8192, 'slab': 1024})

    def test_stat_keeps_file_mapped_and_kernel(self):
        stat = parse_memory_stat(['file 5000', 'file_mapped 1000', 'kernel 2000'])
        self.assertEqual(stat['file_mapped'], 1000)
        self.assertEqual(stat['kernel'], 2000)

    def test_pressure(self):
        lines = ['some avg10=0.42 avg60=0.10 avg300=0.02 total=123456',
                 'full avg10=0.00 avg60=0.00 avg300=0.00 total=0']
        pressure = parse_memory_pressure(lines)
        self.assertAlmostEqual(pressure['some']['avg10'], 0.42)
        self.assertEqual(pressure['some']['total'], 123456)
        self.assertIn('full', pressure)

    def test_pressure_ignores_junk(self):
        self.assertEqual(parse_memory_pressure(['nonsense', 'some']), {})


class TestCGroupRead(unittest.TestCase):
    """The CGroup reader against a temporary cgroup root."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.cg = CGroup('/system.slice/foo.service')
        self.cg.root = tmp.name
        self.target = os.path.join(tmp.name, 'system.slice', 'foo.service')
        os.makedirs(self.target)

    def _write(self, name, content):
        with open(os.path.join(self.target, name), 'w', encoding='utf-8') as fh:
            fh.write(content)

    def test_read_all(self):
        self._write('memory.current', '1048576\n')
        self._write('memory.stat',
                    'anon 524288\nfile 262144\nfile_mapped 65536\n'
                    'kernel 4096\nslab 2048\n')
        self._write('memory.pressure',
                    'some avg10=1.50 avg60=0.5 avg300=0.1 total=10\n')
        data = self.cg.read()
        self.assertEqual(data['current'], 1048576)
        self.assertEqual(data['stat']['anon'], 524288)
        self.assertEqual(data['stat']['file_mapped'], 65536)
        self.assertEqual(data['stat']['kernel'], 4096)
        self.assertAlmostEqual(data['pressure']['some']['avg10'], 1.5)

    def test_read_missing_files(self):
        data = self.cg.read()
        self.assertIsNone(data['current'])
        self.assertEqual(data['stat'], {})
        self.assertEqual(data['pressure'], {})

    def test_read_absent_directory(self):
        cg = CGroup('/does/not/exist')
        cg.root = self.cg.root
        data = cg.read()
        self.assertIsNone(data['current'])
        self.assertEqual(data['stat'], {})


class TestHierarchy(unittest.TestCase):
    """Descendant/local logic for cgroup v2 rows and totals."""

    def test_path_is_descendant(self):
        self.assertTrue(path_is_descendant('/a/b/c', '/a/b'))
        self.assertTrue(path_is_descendant('/a/b/c', '/a'))
        self.assertTrue(path_is_descendant('/a/b/c', '/'))
        self.assertFalse(path_is_descendant('/a/b', '/a/b'))
        self.assertFalse(path_is_descendant('/a/bc', '/a/b'))
        self.assertFalse(path_is_descendant('/a', '/a/b'))

    def test_has_descendant(self):
        keys = {'/a', '/a/b', '/a/b/c', '/x'}
        self.assertTrue(has_descendant(keys, '/a'))
        self.assertTrue(has_descendant(keys, '/a/b'))
        self.assertFalse(has_descendant(keys, '/a/b/c'))
        self.assertFalse(has_descendant(keys, '/x'))

    def test_local_values(self):
        raw = {'/a': {'cache': 100, 'kmem': 10, 'kcharge': 200},
               '/a/b': {'cache': 60, 'kmem': 6, 'kcharge': 120},
               '/a/b/c': {'cache': 20, 'kmem': 2, 'kcharge': 40},
               '/x': {'cache': 7, 'kmem': 1, 'kcharge': 9}}
        local = local_values(raw, ('cache', 'kmem', 'kcharge'))
        self.assertEqual(local['/a']['cache'], 20)
        self.assertEqual(local['/a/b']['cache'], 40)
        self.assertEqual(local['/a/b/c']['cache'], 20)
        self.assertEqual(local['/x']['cache'], 7)
        # the local values reproduce the true (non-double-counted) total
        self.assertEqual(sum(v['cache'] for v in local.values()), 87)
        self.assertEqual(sum(v['kcharge'] for v in local.values()), 169)

if __name__ == '__main__':
    unittest.main()
