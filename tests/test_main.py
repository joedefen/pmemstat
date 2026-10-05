"""Unit tests for the pure logic in :mod:`pmemstat.main`.

These tests deliberately avoid the curses UI and any live ``/proc`` scanning
(except where a test reads the current process); they exercise the small,
deterministic functions that do the memory math and the smaps/rollup parsing.

Importing the ``tests`` package (which Python does before importing this
module) installs a ``console_window`` stub when the real package is missing.
"""
import os
import unittest
from types import SimpleNamespace

from pmemstat.main import (human, compute_zram_effective, ProcMem,
                           auto_sudo_opted_in, AUTO_SUDO_ENV)

GIB = 1024 ** 3
FIXTURES = os.path.join(os.path.dirname(__file__), 'fixtures')


def make_chunk(**kwargs):
    """Build a chunk shaped like those produced by ``ProcMem.make_chunks``."""
    chunk = SimpleNamespace(**ProcMem.chunk_dict)
    for key, val in kwargs.items():
        setattr(chunk, key, val)
    return chunk


class TestHuman(unittest.TestCase):
    """``human()`` renders a byte/KB count as a short suffixed string."""

    def test_non_positive_is_zero(self):
        self.assertEqual(human(0), 0)
        self.assertEqual(human(-1), 0)

    def test_units(self):
        self.assertEqual(human(1024), '1.0K')
        self.assertEqual(human(1536), '1.5K')
        self.assertEqual(human(1024 ** 2), '1.0M')
        self.assertEqual(human(1024 ** 3), '1.0G')
        self.assertEqual(human(1024 ** 4), '1.0T')

    def test_boundary_rolls_to_next_unit(self):
        # 1000 KiB is >= 999.95, so it rolls over to MiB.
        self.assertEqual(human(1000 * 1024), '1.0M')
        self.assertEqual(human(999 * 1024), '999.0K')


class TestComputeZramEffective(unittest.TestCase):
    """``compute_zram_effective()`` is a portable, dependency-free function."""

    def test_compression_working(self):
        result = compute_zram_effective(
            16 * GIB, 8 * GIB, 8 * GIB, 4 * GIB, 1 * GIB, 8 * GIB)
        self.assertAlmostEqual(result.e_used, 11 * GIB)
        self.assertAlmostEqual(result.ratio_current, 4.0)
        self.assertAlmostEqual(result.usage_fraction, 0.5)
        self.assertEqual(result.projection_confidence, 'high')
        self.assertAlmostEqual(result.ratio_projected, 3.8)
        # e_max_used = min(projected*total*limit, disksize)
        #              + (total - that/projected)
        e_max = min(3.8 * 16 * 0.8, 8) + (16 - 8 / 3.8)
        self.assertAlmostEqual(result.e_max_used, e_max * GIB)
        self.assertAlmostEqual(result.e_avail, (e_max - 11) * GIB)

    def test_zram_not_helping_falls_back(self):
        result = compute_zram_effective(
            16 * GIB, 8 * GIB, 8 * GIB, 0.5 * GIB, 1 * GIB, 8 * GIB)
        self.assertAlmostEqual(result.e_used, 8 * GIB)
        self.assertAlmostEqual(result.ratio_current, 2.75)
        self.assertAlmostEqual(result.usage_fraction, 0.0625)
        self.assertEqual(result.projection_confidence, 'low')
        self.assertAlmostEqual(result.ratio_projected, 2.75 * 0.75)

    def test_mem_limit_clamps_limit_pct(self):
        result = compute_zram_effective(
            16 * GIB, 8 * GIB, 8 * GIB, 4 * GIB, 1 * GIB, 8 * GIB,
            zram_mem_limit=2 * GIB)
        # limit_pct = min(100, 80, 2/16*100 = 12.5) = 12.5
        raw = min(3.8 * 16 * 0.125, 8)
        e_max = raw + (16 - raw / 3.8)
        self.assertAlmostEqual(result.e_max_used, e_max * GIB)

    def test_zero_disksize(self):
        result = compute_zram_effective(
            16 * GIB, 8 * GIB, 8 * GIB, 4 * GIB, 1 * GIB, 0)
        self.assertAlmostEqual(result.usage_fraction, 0)
        self.assertEqual(result.projection_confidence, 'low')


class TestMakeChunks(unittest.TestCase):
    """Parse raw ``smaps`` text into chunk objects."""

    def setUp(self):
        self.pm = ProcMem(1234)

    def test_single_section(self):
        lines = [
            '00400000-004b8000 r-xp 00000000 fd:00 11143998     /usr/bin/foo',
            'Size:                736 kB',
            'Rss:                 592 kB',
            'Pss:                  87 kB',
            'Shared_Clean:        592 kB',
            'Shared_Dirty:          0 kB',
            'Private_Clean:         0 kB',
            'Swap:                  0 kB',
            'THPeligible:           0',
        ]
        chunks = self.pm.make_chunks(lines)
        self.assertEqual(len(chunks), 1)
        self.assertEqual(self.pm.parse_err_cnt, 0)
        chunk = chunks[0]
        self.assertEqual(chunk.beg, 0x400000)
        self.assertEqual(chunk.end, 0x4b8000)
        self.assertEqual(chunk.perms, 'r-xp')
        self.assertEqual(chunk.offset, 0)
        self.assertEqual(chunk.item, '/usr/bin/foo')
        self.assertEqual(chunk.size, 736)
        self.assertEqual(chunk.rss, 592)
        self.assertEqual(chunk.pss, 87)
        self.assertEqual(chunk.shared, 592)
        self.assertEqual(chunk.private, 0)
        self.assertEqual(chunk.swap, 0)

    def test_multiple_sections_and_junk_lines(self):
        lines = [
            '00400000-004b8000 r-xp 00000000 fd:00 1     /usr/bin/foo',
            'Size:                  4 kB',
            'Rss:                   4 kB',
            'Pss:                   4 kB',
            'VmFlags: rd ex mr mw me',
            '7f000000-7f001000 rw-p 00000000 00:00 0      [heap]',
            'Size:                  8 kB',
            'Rss:                   8 kB',
            'Pss:                   8 kB',
            'Private_Dirty:         8 kB',
            'ProtectionKey:         0',
        ]
        chunks = self.pm.make_chunks(lines)
        self.assertEqual(len(chunks), 2)
        self.assertEqual(self.pm.parse_err_cnt, 0)
        self.assertEqual(chunks[1].perms, 'rw-p')
        self.assertEqual(chunks[1].item, '[heap]')
        self.assertEqual(chunks[1].private, 8)


class TestCategorizeChunks(unittest.TestCase):
    """``categorize_chunks()`` assigns a category and effective size."""

    def setUp(self):
        self.pm = ProcMem(1)

    def _categorize(self, chunk):
        chunks = [chunk]
        self.pm.categorize_chunks(chunks)
        return chunk

    def test_shared_sysv(self):
        chunk = self._categorize(
            make_chunk(perms='rw-s', item='/SYSV00000000 (deleted)', pss=11))
        self.assertEqual(chunk.cat, 'shSYSV')
        self.assertEqual(chunk.eSize, 11)

    def test_shared_other(self):
        chunk = self._categorize(
            make_chunk(perms='r-s-', item='/usr/lib/libc.so', pss=22))
        self.assertEqual(chunk.cat, 'shOth')
        self.assertEqual(chunk.eSize, 22)

    def test_stack(self):
        chunk = self._categorize(
            make_chunk(perms='rw-p', item='[stack]', private=33))
        self.assertEqual(chunk.cat, 'stack')
        self.assertEqual(chunk.eSize, 33)

    def test_unwriteable_guard_page_is_data_zero(self):
        # size must not be 4 to avoid the special stack-pair heuristic.
        chunk = self._categorize(
            make_chunk(perms='---p', item='', size=8, offset=0, beg=0))
        self.assertEqual(chunk.cat, 'data')
        self.assertEqual(chunk.eSize, 0)

    def test_writable_is_data_rss_plus_swap(self):
        chunk = self._categorize(
            make_chunk(perms='rw-p', item='', rss=40, swap=2))
        self.assertEqual(chunk.cat, 'data')
        self.assertEqual(chunk.eSize, 42)

    def test_readonly_is_text_pss_plus_swap(self):
        chunk = self._categorize(
            make_chunk(perms='r-xp', item='/usr/bin/foo', pss=50, swap=5))
        self.assertEqual(chunk.cat, 'text')
        self.assertEqual(chunk.eSize, 55)


class TestSummarizeChunks(unittest.TestCase):
    """``summarize_chunks()`` accumulates chunk sizes into the summary dict."""

    def test_sums_by_category(self):
        pm = ProcMem(4321)
        chunks = [
            make_chunk(cat='data', eSize=100, pswap=5),
            make_chunk(cat='text', eSize=50, pswap=2),
            make_chunk(cat='stack', eSize=25, pswap=0),
        ]
        summary = pm.summarize_chunks(chunks)
        self.assertEqual(summary['data'], 100)
        self.assertEqual(summary['text'], 50)
        self.assertEqual(summary['stack'], 25)
        self.assertEqual(summary['ptotal'], 175)
        self.assertEqual(summary['pswap'], 7)
        self.assertEqual(summary['number'], -4321)


class TestParseRollups(unittest.TestCase):
    """``parse_rollups()`` maps ``smaps_rollup`` fields into a summary."""

    LINES = [
        'Rss:                2400 kB',
        'Pss:                 341 kB',
        'Pss_Anon:            160 kB',
        'Pss_File:            181 kB',
        'Pss_Shmem:             0 kB',
        'SwapPss:               0 kB',
        'AnonHugePages:         0 kB',
    ]

    def _parse(self, lines, has_zram):
        pm = ProcMem(1)
        ProcMem.pmemstat = SimpleNamespace(has_zram=lambda: has_zram)
        try:
            return pm.parse_rollups(lines)
        finally:
            ProcMem.pmemstat = None

    def test_without_zram(self):
        summary = self._parse(self.LINES, has_zram=False)
        self.assertEqual(summary['data'], 160)
        self.assertEqual(summary['text'], 181)
        self.assertEqual(summary['shOth'], 0)
        self.assertEqual(summary['ptotal'], 341)
        self.assertEqual(summary['pss'], 341)

    def test_with_zram_includes_pswap(self):
        summary = self._parse(
            ['Pss_Anon:  100 kB', 'SwapPss:  50 kB'], has_zram=True)
        self.assertEqual(summary['pswap'], 50)
        self.assertEqual(summary['ptotal'], 150)
        self.assertEqual(summary['pss'], 150)


class TestKernelFixtures(unittest.TestCase):
    """Regression guard against kernel-format drift in smaps parsing.

    The fixtures were captured from a real ``/proc/<pid>/smaps`` and
    ``smaps_rollup`` on a Linux 6.12 kernel.  If a future kernel adds fields,
    these tests will surface the parser drift (``parse_err_cnt``).
    """

    def test_smaps_fixture_parses_cleanly(self):
        pm = ProcMem(1)
        with open(os.path.join(FIXTURES, 'smaps_sample.txt'),
                  encoding='utf-8') as fh:
            lines = fh.read().splitlines()
        chunks = pm.make_chunks(lines)
        self.assertEqual(pm.parse_err_cnt, 0,
                         'parser could not understand some smaps lines')
        self.assertGreater(len(chunks), 20)
        pm.categorize_chunks(chunks)
        self.assertTrue(all(chunk.cat for chunk in chunks))
        summary = pm.summarize_chunks(chunks)
        self.assertGreater(summary['ptotal'], 0)

    def test_rollup_fixture_parses_cleanly(self):
        pm = ProcMem(1)
        ProcMem.pmemstat = SimpleNamespace(has_zram=lambda: False)
        try:
            with open(os.path.join(FIXTURES, 'smaps_rollup_sample.txt'),
                      encoding='utf-8') as fh:
                lines = fh.read().splitlines()
            summary = pm.parse_rollups(lines)
        finally:
            ProcMem.pmemstat = None
        self.assertEqual(summary['pss'],
                         summary['data'] + summary['text'] + summary['shOth'])


class TestAutoSudoOptIn(unittest.TestCase):
    """Auto-elevation is strictly opt-in via the PMEMSTAT_AUTO_SUDO variable."""

    def _with_env(self, value):
        saved = os.environ.get(AUTO_SUDO_ENV)
        if value is None:
            os.environ.pop(AUTO_SUDO_ENV, None)
        else:
            os.environ[AUTO_SUDO_ENV] = value
        try:
            return auto_sudo_opted_in()
        finally:
            if saved is None:
                os.environ.pop(AUTO_SUDO_ENV, None)
            else:
                os.environ[AUTO_SUDO_ENV] = saved

    def test_unset_is_off(self):
        self.assertFalse(self._with_env(None))

    def test_empty_is_off(self):
        self.assertFalse(self._with_env(''))

    def test_falsy_values_are_off(self):
        for value in ('0', 'false', 'False', 'no', 'off', '  OFF  '):
            self.assertFalse(self._with_env(value), value)

    def test_truthy_values_are_on(self):
        for value in ('1', 'true', 'TRUE', 'yes', 'on', 'anything'):
            self.assertTrue(self._with_env(value), value)


if __name__ == '__main__':
    unittest.main()
