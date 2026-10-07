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

from pmemstat.main import (human, human_kb, ago_str, GrowthTracker,
                           compute_zram_effective, ProcMem, PmemStat,
                           args_from_env, ARGS_ENV)

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
        # three+ leading digits drop the decimal (truncated, not rounded)
        self.assertEqual(human(999 * 1024), '999K')

    def test_decimal_dropped_at_three_digits(self):
        self.assertEqual(human(100 * 1024), '100K')
        self.assertEqual(human(int(99.9 * 1024)), '99.9K')
        self.assertEqual(human(int(10.1 * 1024)), '10.1K')


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


class TestEnvArgs(unittest.TestCase):
    """``PMEMSTAT_ARGS`` is tokenized like a POSIX shell command line."""

    def _with_env(self, value):
        saved = os.environ.get(ARGS_ENV)
        if value is None:
            os.environ.pop(ARGS_ENV, None)
        else:
            os.environ[ARGS_ENV] = value
        try:
            return args_from_env()
        finally:
            if saved is None:
                os.environ.pop(ARGS_ENV, None)
            else:
                os.environ[ARGS_ENV] = saved

    def test_unset_is_empty(self):
        self.assertEqual(self._with_env(None), [])

    def test_empty_is_empty(self):
        self.assertEqual(self._with_env(''), [])

    def test_simple_split(self):
        self.assertEqual(self._with_env('--psi --loop 3 -s name'),
                         ['--psi', '--loop', '3', '-s', 'name'])

    def test_quotes_are_honored(self):
        self.assertEqual(self._with_env('--search "two words"'),
                         ['--search', 'two words'])

    def test_unbalanced_quotes_raise(self):
        with self.assertRaises(ValueError):
            self._with_env('--search "oops')


class TestAgoStr(unittest.TestCase):
    """``ago_str()`` renders a compact, at-most-two-component interval."""

    def test_zero_and_seconds(self):
        self.assertEqual(ago_str(0), '0s')
        self.assertEqual(ago_str(45), '45s')

    def test_one_minute_plus_seconds(self):
        self.assertEqual(ago_str(90), '1m30s')

    def test_exact_values_drop_zero_lower_unit(self):
        self.assertEqual(ago_str(120), '2m')
        self.assertEqual(ago_str(3600), '1h')
        self.assertEqual(ago_str(86400), '1d')

    def test_two_components(self):
        self.assertEqual(ago_str(18 * 3600 + 39 * 60), '18h39m')
        self.assertEqual(ago_str(86400 + 3600), '1d1h')

    def test_negative_uses_magnitude(self):
        self.assertEqual(ago_str(-90), '1m30s')


class TestHumanKb(unittest.TestCase):
    """``human_kb()`` is the byte-based ``human()`` fed KB."""

    def test_kb_to_human(self):
        self.assertEqual(human_kb(1), '1.0K')
        self.assertEqual(human_kb(1024), '1.0M')
        self.assertEqual(human_kb(1024 * 1024), '1.0G')


class TestGrowthTracker(unittest.TestCase):
    """The geometric multi-resolution baseline math."""

    def test_no_growth_before_first_anchor(self):
        tracker = GrowthTracker()
        self.assertIsNone(tracker.update(0.0, 100))

    def test_growth_after_first_anchor(self):
        tracker = GrowthTracker()
        tracker.update(0.0, 100)
        self.assertEqual(tracker.update(20.0, 150), (50, 4.0))

    def test_negative_growth_is_clamped(self):
        tracker = GrowthTracker()
        tracker.update(0.0, 100)
        self.assertEqual(tracker.update(20.0, 80), (0, 4.0))

    def test_zero_interval_at_anchor_returns_none(self):
        tracker = GrowthTracker()
        tracker.update(0.0, 100)
        self.assertIsNone(tracker.update(16.0, 100))

    def test_anchor_values_are_non_decreasing(self):
        tracker = GrowthTracker()
        tracker.update(0.0, 200)
        tracker.update(20.0, 50)
        # the anchor recorded for 16s keeps the 200 high-water mark
        self.assertEqual(tracker.snaps[0], (16, 200))

    def test_rebase_moves_to_newer_anchor(self):
        tracker = GrowthTracker()
        tracker.update(0.0, 100)
        tracker.update(20.0, 200)
        tracker.update(300.0, 400)
        self.assertEqual(tracker.snaps[0], (16, 100))
        # age 600 >= 32*16, so the 16s anchor is dropped
        self.assertEqual(tracker.update(600.0, 500), (300, 536.0))
        self.assertEqual(tracker.snaps[0], (64, 200))


class TestDebouncedWidth(unittest.TestCase):
    """Growth-column width grows at once and shrinks with hysteresis."""

    # pylint: disable=protected-access

    def setUp(self):
        self.pm = PmemStat(SimpleNamespace(units='MB', debug=0))

    def test_grows_immediately(self):
        """A wider need is adopted at once."""
        self.assertEqual(self.pm._debounced_width(1, 5, 0), (5, 0))

    def test_shrink_waits(self):
        """A narrower need keeps the old width until the delay elapses."""
        self.assertEqual(self.pm._debounced_width(5, 2, 0), (5, 1))
        self.assertEqual(self.pm._debounced_width(5, 2, 2), (5, 3))

    def test_shrinks_after_delay(self):
        """The width shrinks once enough smaller refreshes accrue."""
        delay = PmemStat.WIDTH_SHRINK_DELAY
        self.assertEqual(self.pm._debounced_width(5, 2, delay - 1), (2, 0))

    def test_equal_resets_count(self):
        """An unchanged need clears the smaller-refresh counter."""
        self.assertEqual(self.pm._debounced_width(5, 5, 3), (5, 0))


class TestRateFloor(unittest.TestCase):
    """Rate mode hides the wild short-window projection."""

    def setUp(self):
        self.pm = PmemStat(SimpleNamespace(units='MB', debug=0,
                                           growth_style='rate'))

    def test_rate_hidden_before_floor(self):
        """A 5s window is not projected to a day."""
        group = SimpleNamespace(growth_val=1000, growth_interval=5.0,
                                growth_rate=12000.0)
        self.assertEqual(self.pm.growth_text(group), '')

    def test_rate_shown_after_floor(self):
        """Once past the floor the value is a per-day rate."""
        group = SimpleNamespace(growth_val=1000, growth_interval=120.0,
                                growth_rate=500.0)
        self.assertTrue(self.pm.growth_text(group).endswith('/d'))

    def test_system_line_hides_rate_before_floor(self):
        """The system line shows no /d until the rate floor is reached."""
        self.pm.sys_growth = {'Used': (1000, 5.0)}
        self.assertNotIn('/d', self.pm.format_sys_growth())


class TestCgroupLabel(unittest.TestCase):
    """Launcher-named app scopes are relabelled by their dominant app."""

    def setUp(self):
        self.pm = PmemStat(SimpleNamespace(units='MB', debug=0))

    def _group(self, key, exes):
        prcset = [SimpleNamespace(exebasename=e) for e in exes]
        return SimpleNamespace(key=key, prcset=prcset)

    def test_keeps_leaf_when_a_member_matches(self):
        """A firefox scope keeps its systemd name."""
        group = self._group('/app.slice/app-niri-firefox-3646.scope',
                            ['firefox', 'firefox'])
        self.assertEqual(self.pm.cgroup_label(group),
                         'app-niri-firefox-3646.scope')

    def test_keeps_leaf_for_proper_app_id(self):
        """A vivaldi scope keeps its name (member prefix matches)."""
        group = self._group('/x/app-com.vivaldi.Vivaldi-18013.scope',
                            ['vivaldi-stable'])
        self.assertEqual(self.pm.cgroup_label(group),
                         'app-com.vivaldi.Vivaldi-18013.scope')

    def test_replaces_launcher_scope_with_dominant_app(self):
        """A fuzzel scope hosting code is relabelled 'code'."""
        group = self._group('/x/app-niri-fuzzel-32513.scope',
                            ['code', 'code', 'vim'])
        self.assertEqual(self.pm.cgroup_label(group), 'code')

    def test_plus_marker_preserved(self):
        """A relabelled row still gets the descendant '+' marker."""
        self.pm.cgroup_keys = {'/x', '/x/app-niri-fuzzel-32513.scope'}
        group = self._group('/x', ['code'])
        self.assertEqual(self.pm.cgroup_label(group), 'code+')


if __name__ == '__main__':
    unittest.main()
