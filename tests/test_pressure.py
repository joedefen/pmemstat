#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tests for the Linux PSI helpers in pmemstat.Pressure.

These tests never touch the real ``/proc``: the parsers take text and the
reader's ``root`` is redirected to a temporary directory.
"""

import os
import tempfile
import unittest

from pmemstat.Pressure import (format_pressure_lines, parse_pressure,
                               read_system_pressure)

SOME = 'some avg10=0.42 avg60=0.10 avg300=0.02 total=123456'
FULL = 'full avg10=0.00 avg60=0.00 avg300=0.00 total=0'


class TestParsePressure(unittest.TestCase):
    """The /proc/pressure/* parser (shared grammar with memory.pressure)."""

    def test_some_and_full(self):
        pressure = parse_pressure([SOME, FULL])
        self.assertAlmostEqual(pressure['some']['avg10'], 0.42)
        self.assertAlmostEqual(pressure['some']['avg60'], 0.10)
        self.assertAlmostEqual(pressure['some']['avg300'], 0.02)
        self.assertEqual(pressure['some']['total'], 123456)
        self.assertIn('full', pressure)
        self.assertEqual(pressure['full']['total'], 0)

    def test_cpu_has_only_some(self):
        pressure = parse_pressure([SOME])
        self.assertEqual(set(pressure), {'some'})

    def test_ignores_junk(self):
        self.assertEqual(parse_pressure(['nonsense', 'some']), {})
        self.assertEqual(parse_pressure(['other avg10=1.0']), {})

    def test_ignores_malformed_tokens(self):
        pressure = parse_pressure(['some avg10 avg60=x total=7'])
        self.assertEqual(pressure['some'], {'total': 7})


class TestReadSystemPressure(unittest.TestCase):
    """read_system_pressure() against a temporary pressure directory."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = tmp.name

    def _write(self, name, content):
        with open(os.path.join(self.root, name), 'w', encoding='utf-8') as fh:
            fh.write(content)

    def test_reads_available_resources(self):
        self._write('memory', SOME + '\n' + FULL + '\n')
        self._write('cpu', SOME + '\n')
        snapshot = read_system_pressure(self.root)
        self.assertEqual(set(snapshot), {'memory', 'cpu'})
        self.assertAlmostEqual(snapshot['memory']['some']['avg10'], 0.42)
        self.assertIn('full', snapshot['memory'])
        self.assertNotIn('full', snapshot['cpu'])

    def test_missing_files_omitted(self):
        # No PSI files at all (CONFIG_PSI=n) -> empty snapshot.
        self.assertEqual(read_system_pressure(self.root), {})

    def test_absent_directory(self):
        self.assertEqual(
            read_system_pressure(os.path.join(self.root, 'nope')), {})


class TestFormatPressureLines(unittest.TestCase):
    """format_pressure_lines() renders the fixed-width PSI table."""

    SNAPSHOT = {
        'memory': {'some': {'avg10': 1.23, 'avg60': 0.45, 'avg300': 0.2},
                   'full': {'avg10': 0.1, 'avg60': 0.05, 'avg300': 0.02}},
        'cpu': {'some': {'avg10': 0.0, 'avg60': 0.03, 'avg300': 0.0}},
        'io': {'some': {'avg10': 0.15, 'avg60': 0.6, 'avg300': 1.04},
               'full': {'avg10': 0.1, 'avg60': 0.49, 'avg300': 0.96}},
    }

    def test_empty_snapshot_is_empty_list(self):
        self.assertEqual(format_pressure_lines({}), [])

    def test_four_line_table(self):
        lines = format_pressure_lines(self.SNAPSHOT)
        self.assertEqual(len(lines), 4)
        self.assertTrue(lines[0].startswith('PSI'))
        self.assertIn('SOME', lines[0])
        self.assertIn('FULL', lines[0])
        for label in ('10s', '60s', '300s'):
            self.assertIn(label, lines[0])
        self.assertNotIn('(', lines[0])
        # one row per resource, in memory/cpu/io order
        self.assertTrue(lines[1].startswith('    mem%'))
        self.assertTrue(lines[2].startswith('    cpu%'))
        self.assertTrue(lines[3].startswith('     io%'))
        self.assertIn('1.23', lines[1])
        self.assertIn('1.04', lines[3])

    def test_rows_are_aligned_to_same_width(self):
        widths = {len(line) for line in
                  format_pressure_lines(self.SNAPSHOT)}
        self.assertEqual(len(widths), 1)

    def test_missing_full_line_shows_dash(self):
        # cpu has no full line on most kernels -> '-' cells in its full group.
        lines = format_pressure_lines(self.SNAPSHOT)
        cpu_row = lines[2]
        self.assertIn('     -', cpu_row)
        # memory has both groups, so no dash on its row.
        self.assertNotIn('-', lines[1])

    def test_absent_resource_skipped(self):
        lines = format_pressure_lines({'cpu': self.SNAPSHOT['cpu']})
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[1].startswith('    cpu%'))


if __name__ == '__main__':
    unittest.main()
