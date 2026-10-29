#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Pending Features:
  - Iffy:
    - PSI Page (maybe with a thread)?

Copyright (c) 2022-2023 Joe Defen

NOTE: to create a single file standalone, run:
    stickytape pmemstat.py --copy-shebang > pmemstat && chmod +x pmemstat

A program to aggregate the memory used by processes into categories
so that the memory footprint of processes is more clear.

The lines of smaps look as sequences of the lines shown
below. We call the 1st line a "section line" and those following "item lines".

    00400000-004b8000 r-xp 00000000 fd:00 11143998     /opt/.../inetrep
    Size:                736 kB
    Rss:                 592 kB
    Pss:                  87 kB
    Shared_Clean:        592 kB
    Shared_Dirty:          0 kB
    Private_Clean:         0 kB
    Referenced:          592 kB
    Anonymous:             0 kB
    AnonHugePages:         0 kB
    Swap:                  0 kB
    KernelPageSize:        4 kB
    MMUPageSize:           4 kB

NOTE: kB is a misnomer ... should be "KB".  Morons.
"""
# pylint: disable=broad-except,import-outside-toplevel,global-statement
# pylint: disable=too-many-boolean-expressions,invalid-name
# pylint: disable=too-many-instance-attributes,too-many-lines
# pylint: disable=too-many-arguments,too-many-branches
# pylint: disable=too-many-statements,too-many-locals
# pylint: disable=multiple-statements,too-few-public-methods
# pylint: disable=too-many-public-methods


import os
import re
import shlex
import signal
import subprocess
import sys
import traceback
import time
import curses
# from curses.textpad import rectangle
from types import SimpleNamespace
from io import StringIO
from datetime import datetime, timedelta, timezone
# console-window is intentionally pinned to an exact version (see pyproject.toml).
# Provide a clear, actionable message instead of a bare ImportError when the
# pinned package is missing or when a different version has been substituted.
# Keep this in sync with the pin in pyproject.toml's ``dependencies``.
from console_window import (ConsoleWindow, ConsoleWindowOpts,
                            OptionSpinner, IncrementalSearchBar,
                            InlineConfirmation, Theme, Screen, ScreenStack)
from pmemstat.KillThem import KillThem
from pmemstat.CpuSmooth import CpuSmooth, SysStat
from pmemstat import History
from pmemstat.CGroup import (CGroup, cgroup_leaf, has_descendant,
                             local_values, parse_cgroup_lines)
from pmemstat.Pressure import format_pressure_lines, read_system_pressure
from pmemstat.UiPrefs import UiPrefs

# Trace Levels:
#  0 - forced, temporary debugging (comment it out)
#  1+ - regular debugging (higher is less important and/or more verbose)

DebugLevel = 0

read_smaps = 0

def DB(level, *opts, **kwargs):
    """Debug message printer.
    - printing is conditional on DebugLevel being no smaller than the passed level
    - level 0 is unconditional. It is use for temporary traces that
      are commented out when the debug need is gone.
    """
    # pylint: disable=protected-access
    # print(f'DbLevel={DebugLevel} level={level} do_debug={bool(DebugLevel>=level)}')
    if DebugLevel >= level:
        lineno = sys._getframe(1).f_lineno
        tstr = StringIO()
        print(f'DB{level}', end=' ', file=tstr)
        kwargs['end'] = ' '
        kwargs['file'] = tstr
        print(*opts, **kwargs)
        print(tstr.getvalue() + f'[:{lineno}]')

# Grouping methods that present cgroup v2 rows. 'cgroup' is the footprint view
# (the default) and 'cgroupCharge' is the same grouping with the kernel
# ``memory.current`` charge shown instead; they differ only in the memory view.
CGROUP_GROUPS = ('cgroup', 'cgroupCharge')

# Every grouping whose growth history is tracked independently, so that changing
# the grouping never resets the leak baseline (see PmemStat.update_growth).
GROWTH_VIEWS = ('exe', 'cmd', 'pid') + CGROUP_GROUPS

# Set PMEMSTAT_NO_HISTORY to any value to disable cross-run history entirely.
HISTORY_ENV_DISABLE = 'PMEMSTAT_NO_HISTORY'

# pmemstat's own "no colour at all" pseudo-theme.  console-window has no such
# theme (its 'dark-mono'/'light-mono' are still colour themes), so mono lives
# here: every semantic role degrades to an attribute instead of a colour pair.
MONO_THEME = 'mono'

# Screen identities for the ScreenStack; MAIN is the report itself.
MAIN_ST, HELP_ST, THEMES_ST = 0, 1, 2
SCREEN_NAMES = ('MAIN', 'HELP', 'THEMES')


def is_cgroup_groupby(groupby):
    """True when ``groupby`` selects a cgroup v2 grouping (either view)."""
    return groupby in CGROUP_GROUPS


def cgroup_view_totals(data):
    """Raw (hierarchical) ``footprt``/``kcharge`` totals in KB from a read.

    ``data`` is a :meth:`pmemstat.CGroup.CGroup.read` result. Both totals come
    from the same file access so the two cgroup groupings can share one read.
    Returns ``None`` when the cgroup has no ``memory.current`` (nothing to
    charge):

      * ``footprt`` = ``current - inactive_file - slab_reclaimable + swap``
        (the derived footprint, i.e. what the cgroup really holds).
      * ``kcharge`` = ``current`` (the kernel's own charge).
    """
    stat = data['stat'] or {}
    if data['current'] is None:
        return None
    current = data['current']
    swap = stat.get('swap')
    if swap is None:
        swap = data.get('swap_current') or 0
    inactive_file = stat.get('inactive_file', 0)
    slab_reclaimable = stat.get('slab_reclaimable', 0)
    return {
        'footprt': max(0, (current - inactive_file
                           - slab_reclaimable + swap) // 1024),
        'kcharge': current // 1024,
    }


##############################################################################
##   Colour roles
##############################################################################
# Colour is decoration, never information: every role degrades to "no
# attribute" (plain text) when the terminal has no colour.  The pair ids are
# console-window's semantic Theme roles, so a theme change recolours the whole
# report without an edit here.  Roles used:
#   HOTSWAP  - numbers that are *changing*: a row's growth annotation, the Δ
#              values on the system growth line, the 'growth' header label
#   EMPHASIS - titles: the column header line and the TOTALS row.  Deliberately
#              NOT every row's grouping name: the stock 'default' theme renders
#              EMPHASIS as white on the terminal's default background, which is
#              invisible on a light terminal, and the key/info column is the
#              densest text in the report.
#   INFO     - the zRAM estimate block (effective RAM, compression ratio)
#   SUCCESS  - the 'A' marker on a group that has just appeared
#   DANGER   - an irreversible action about to happen (kill / growth reset)
#   A_DIM    - labels, meta text, zero-valued cells and 'n/a' (skipped by eye)
# Role -> attribute used when colour is off (the 'mono' theme).  The hierarchy
# is preserved with emphasis instead of hue: changing numbers and titles are
# bold, destructive prompts are bold+reversed, and informational values carry
# nothing (their dim labels already distinguish them).
MONO_ROLES = {
    Theme.HOTSWAP: curses.A_BOLD,
    Theme.EMPHASIS: curses.A_BOLD,
    Theme.SUCCESS: curses.A_BOLD,
    Theme.WARNING: curses.A_BOLD,
    Theme.DANGER: curses.A_BOLD | curses.A_REVERSE,
    Theme.ERROR: curses.A_BOLD | curses.A_REVERSE,
    Theme.INFO: curses.A_NORMAL,
    Theme.PROGRESS: curses.A_NORMAL,
    Theme.OLD_SUCCESS: curses.A_NORMAL,
}

# Process-wide colour switch, mirroring console-window's glyph-mode policy: one
# terminal, so one decision.  Seeded from the options at startup and flipped by
# the 'mono' entry in the theme picker.
_COLORS_ENABLED = True


def set_colors_enabled(enabled):
    """Turn semantic colour on or off for the whole process ('mono' turns it off)."""
    global _COLORS_ENABLED  # pylint: disable=global-statement
    _COLORS_ENABLED = bool(enabled)


def colors_enabled():
    """True while semantic roles resolve to colour pairs rather than attributes."""
    return _COLORS_ENABLED


def semantic_attr(pair_id):
    """Attribute for a console-window ``Theme`` pair, or 0 when no colour.

    With colour off (the 'mono' theme) the role becomes an attribute from
    :data:`MONO_ROLES` instead, so the report keeps its emphasis hierarchy on a
    monochrome terminal.  Otherwise: both ``curses.has_colors()`` and
    ``curses.color_pair()`` raise ``curses.error`` before ``initscr()``, and the
    one-shot (``-W``) report and the unit tests both run with no screen, so a
    failed lookup must never abort a refresh; it degrades to plain text.
    """
    if not _COLORS_ENABLED:
        return MONO_ROLES.get(pair_id, curses.A_NORMAL)
    try:
        if not curses.has_colors():
            return 0
        return curses.color_pair(pair_id)
    except curses.error:
        return 0


##############################################################################
##   human()
##############################################################################
def human(number):
    """ Return a concise number description."""
    if number <= 0:
        return 0
    suffixes = ['K', 'M', 'G', 'T']
    while suffixes:
        suffix = suffixes.pop(0)
        number /= 1024
        if number < 999.95 or not suffixes:
            # Drop the decimal once there are three+ digits before it
            # (100.0G -> 100G): one column narrower and easier to read.
            # Truncate (not round) so 999.9 stays '999', not '1000'.
            if number >= 100:
                return f'{int(number)}{suffix}'
            return f'{number:.1f}{suffix}'
    return '' # impossible, but make pylint happy


def human_kb(kb):
    """Human form of a KB value (``human()`` expects bytes)."""
    return human(kb * 1024)


def ago_str(delta_secs):
    """Compact two-component interval, e.g. '18h39m', '2m1s', '45s'.

    The lower unit is omitted when it is zero, so exact values show '1h'
    rather than '1h0m'.
    """
    ago = int(max(0, round(abs(delta_secs))))
    divs = (60, 60, 24, 7, 52, 9999999)
    units = ('s', 'm', 'h', 'd', 'w', 'y')
    vals = (ago % 60, int(ago / 60))  # seed with secs, mins
    uidx = 1
    for div in divs[1:]:
        if vals[1] < div:
            break
        vals = (vals[1] % div, int(vals[1] / div))
        uidx += 1
    if vals[1]:
        rv = f'{vals[1]}{units[uidx]}'
        if vals[0]:
            rv += f'{vals[0]}{units[uidx-1]}'
        return rv
    return f'{vals[0]}{units[uidx-1]}'


class GrowthTracker:
    """Geometric multi-resolution growth baseline.

    Anchors are at ``16 * 4**k`` seconds. The oldest anchor still within
    ``RETENTION`` times its own age is the base. Growth is measured against the
    base's high-water mark and clamped at zero; anchor values never decrease.
    The first 16 seconds are never inside the window, so startup bursts are
    excluded by construction. Each anchor roll (rebase) may step the displayed
    growth down ~10%; that is accepted because the rate is invariant, so a real
    leak stays visible.
    """
    BASE = 16
    RETENTION = 32

    def __init__(self):
        self.t0 = None
        self.peak = None
        self.snaps = []          # list of (anchor_age, high_water_value)
        self._next_age = self.BASE
        self.last_seen = None    # monotonic time of the last fed sample

    def reset(self):
        """Forget all growth history (used by zap_history).

        ``last_seen`` is deliberately preserved: it is pruning bookkeeping, not
        history.  Clearing it left every reset tracker with a ``None`` timestamp
        that :meth:`PmemStat.prune_growth_trackers` then subtracted from
        ``now``, crashing with ``float - NoneType`` for keys that were no longer
        being fed.
        """
        self.t0 = None
        self.peak = None
        self.snaps = []
        self._next_age = self.BASE

    def seed(self, now, base_val, peak, interval):
        """Pre-load a baseline carried over from a previous run's ledger.

        ``t0`` is back-dated by ``interval`` so it coincides with the baseline's
        first observation, and the baseline anchor is stored at age ``0``.  The
        reported interval is therefore the time since the baseline -- not the
        time since this session began -- and, because the baseline sits at age
        ``0``, it is never aged out by :data:`RETENTION` (growth stays continuous
        since the first observation).  Normal ``update()`` still clamps growth at
        zero, so ``base_val`` (the lower of baseline/current) is the floor and a
        rebound is measured from it.
        """
        interval = max(1, interval)
        self.t0 = now - interval
        self.peak = peak
        self.snaps = [(0, base_val)]
        self._next_age = max(interval, self.BASE) * 4
        self.last_seen = now

    def update(self, now, value):
        """Feed a sample; return ``(growth, interval)`` in (KB, secs) or None."""
        if value is None:
            return None
        if self.t0 is None:
            self.t0 = now
            self.peak = value
            return None
        age = now - self.t0
        while age >= self._next_age:
            self.snaps.append((self._next_age,
                               self.peak if self.peak is not None else value))
            self._next_age *= 4
        if self.peak is None or value > self.peak:
            self.peak = value
        while (len(self.snaps) > 1 and self.snaps[0][0] > 0
               and age >= self.RETENTION * self.snaps[0][0]):
            self.snaps.pop(0)
        if not self.snaps or age <= self.snaps[0][0]:
            return None
        base_age, base_val = self.snaps[0]
        return max(value - base_val, 0), age - base_age


def _parse_iso(value):
    """Parse an ISO-8601 timestamp into an aware datetime, or ``None``."""
    if not isinstance(value, str):
        return None
    try:
        stamp = datetime.fromisoformat(value)
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp


def _as_datetime(value):
    """Coerce epoch seconds to an aware UTC ``datetime``; pass datetimes through."""
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, timezone.utc)
    return value


def _baseline_parts(entry, now_wall):
    """Return ``(base_kb, interval_secs)`` for a ledger baseline entry, else None.

    ``now_wall`` must already be a ``datetime`` (callers normalize via
    :func:`_as_datetime`).  ``interval_secs`` is the wall-clock time since the
    baseline's ``base_ts`` (at least 1s when the timestamp is unusable).
    """
    if not isinstance(entry, dict):
        return None
    try:
        base = int(entry['base'])
    except (KeyError, TypeError, ValueError):
        return None
    stamp = _parse_iso(entry.get('base_ts'))
    interval = ((now_wall - stamp).total_seconds()
                if stamp is not None else 1.0)
    return base, max(1.0, interval)


def seed_from_baselines(trackers, baselines, samples, now_mono, now_wall):
    """Seed per-``(view, key)`` trackers from ledger baselines and first samples.

    ``trackers`` is the mutable ``{(view, key): GrowthTracker}`` map (missing
    entries are created).  ``baselines`` maps ``view -> {key: {'base': kb,
    'base_ts': iso}}`` exactly as returned by :func:`pmemstat.History.baselines`.
    For every ``(view, key)`` present in BOTH ``baselines`` and the current
    ``samples`` the tracker is pre-loaded with ``base_val = min(base, C)`` and
    ``peak = max(base, C)`` and an interval equal to the wall-clock time since
    the baseline was first seen this boot, so growth is measured across runs from
    the ledger's first observation without ever going negative.  A key missing
    from ``baselines`` is left fresh (no seed).  Returns the set of seeded
    ``(view, key)`` keys.
    """
    seeded = set()
    if not baselines:
        return seeded
    now_wall = _as_datetime(now_wall)
    for view, by_key in (samples or {}).items():
        base_view = baselines.get(view) or {}
        for key, current in by_key.items():
            parts = _baseline_parts(base_view.get(key), now_wall)
            if parts is None:
                continue
            base, interval = parts
            skey = (view, key)
            tracker = trackers.get(skey)
            if tracker is None:
                tracker = GrowthTracker()
                trackers[skey] = tracker
            tracker.seed(now_mono, min(base, current), max(base, current),
                         interval)
            seeded.add(skey)
    return seeded


def seed_sys_from_baselines(trackers, baselines, samples, now_mono, now_wall):
    """Seed the flat system trackers from ledger system baselines.

    Like :func:`seed_from_baselines` but for the unscoped system metrics
    (``Used``/``TOTALS``/``ShTmp``/``Oth``/``OthK``/``OthU``): ``trackers`` and
    ``samples`` are flat ``{key: value}`` maps and ``baselines`` is
    ``{key: {'base': kb, 'base_ts': iso}}``.  Returns the set of seeded keys.
    """
    seeded = set()
    if not baselines:
        return seeded
    now_wall = _as_datetime(now_wall)
    for key, current in (samples or {}).items():
        parts = _baseline_parts(baselines.get(key), now_wall)
        if parts is None:
            continue
        base, interval = parts
        tracker = trackers.get(key)
        if tracker is None:
            tracker = GrowthTracker()
            trackers[key] = tracker
        tracker.seed(now_mono, min(base, current), max(base, current), interval)
        seeded.add(key)
    return seeded

####################################################################################
# PORTABLE FUNCTION - Copy-pasted from zram-advisor
# When updating: copy entire function from zram_advisor/main.py (lines 86-166)
####################################################################################
def compute_zram_effective(meminfo_total, meminfo_used, meminfo_available,
                          zram_orig_data_size, zram_mem_used_total,
                          zram_disksize, zram_mem_limit=0, limit_pct=80):
    """
    Compute effective memory values accounting for zRAM compression.

    This is a PORTABLE function that can be copy-pasted between projects.
    NO class dependencies - all inputs passed as parameters.

    Args:
        meminfo_total: Total physical RAM in bytes
        meminfo_used: Currently used RAM in bytes
        meminfo_available: Available RAM in bytes
        zram_orig_data_size: Uncompressed data size stored in zRAM (bytes)
        zram_mem_used_total: Physical RAM consumed by zRAM including overhead (bytes)
        zram_disksize: Max uncompressed data zRAM will accept (bytes)
        zram_mem_limit: Max RAM zRAM can use (0=unlimited) (bytes)
        limit_pct: Arbitrary limit on % of RAM zRAM should use (default 80%)

    Returns:
        SimpleNamespace with:
            - e_used: Effective memory used (uncompressed equivalent)
            - e_avail: Effective memory available
            - e_max_used: Maximum effective memory at full zRAM capacity
            - ratio_current: Current effective compression ratio
            - ratio_projected: Projected ratio accounting for degradation
            - projection_confidence: 'low', 'medium', or 'high'
            - usage_fraction: Fraction of disksize currently used
    """
    # Calculate effective used memory (what it would be if uncompressed)
    e_used = meminfo_used - zram_mem_used_total + zram_orig_data_size

    # Determine compression ratio
    ratio = None
    if e_used <= meminfo_used:  # zRAM not helping yet
        e_used = meminfo_used
        ratio = 2.75  # Conservative guess for projection when no data yet

    if ratio is None:  # Calculate actual ratio
        ratio = zram_orig_data_size / zram_mem_used_total if zram_mem_used_total > 0 else 2.75

    # Apply memory limit if configured
    if zram_mem_limit > 0:
        stats_limit_pct = (zram_mem_limit / meminfo_total) * 100
        limit_pct = min(100.0, limit_pct, stats_limit_pct)

    # Calculate usage fraction and apply degradation
    usage_fraction = zram_orig_data_size / zram_disksize if zram_disksize > 0 else 0

    # Degradation logic based on current usage
    if usage_fraction < 0.10:
        degradation_factor = 0.75  # Aggressive: little real data
        confidence = "low"
    elif usage_fraction < 0.50:
        degradation_factor = 0.85  # Moderate: some real data
        confidence = "medium"
    else:
        degradation_factor = 0.95  # Conservative: lots of real data
        confidence = "high"

    projected_ratio = ratio * degradation_factor

    # Project maximum effective memory when zRAM fills to limit
    e_max_used = projected_ratio * meminfo_total * limit_pct / 100
    # Cap by disksize limit
    e_max_used = min(e_max_used, zram_disksize)
    # Add uncompressed memory not in zRAM
    e_max_used += meminfo_total - e_max_used / projected_ratio

    # Calculate effective available
    e_avail = e_max_used - e_used

    return SimpleNamespace(
        e_used=e_used,
        e_avail=e_avail,
        e_max_used=e_max_used,
        ratio_current=ratio,
        ratio_projected=projected_ratio,
        projection_confidence=confidence,
        usage_fraction=usage_fraction
    )

####################################################################################
###### ZramProjector class
####################################################################################
class ZramProjector:
    """ Guess the zram numbers """
    def __init__(self):
        self.meminfo = None
        self.e_total = 0
        self.e_avail = 0
        self.e_used = 0
        self.e_max_used = 0
        self.ratio = 0.0
        self.ratio_projected = 0.0
        self.projection_confidence = 'low'
        self.limit_pct = 80
        self.devs = {}
        self.DB = False

    def human_pct(self, number, with_pct=False):
        """ Return the number in human form and as pct total memory. """
        rv = human(number)
        if with_pct and number > 0 and self.meminfo.MemTotal > 0:
            fraction = number/self.meminfo.MemTotal
            pct = int(round(100*fraction))
            rv += f'/{pct}%'
        return rv

    def _get_zram_stats(self):
        """ Get only what we want
         orig_data_size   uncompressed size of data stored in this disk.
         compr_data_size  compressed size of data stored in this disk
         mem_used_total   the amount of memory allocated for this disk.
         mem_limit        max RAM ZRAM can use to store (0 means unlimited)
         mem_used_max     max RAM zram has consumed to store the data
        """
        fields = ('orig_data_size compr_data_size mem_used_total'
                 + ' mem_limit mem_used_max').split()
        infos = {}
        zram_devices = sorted([device for device in os.listdir('/sys/class/block/')
                    if device.startswith('zram')])
        for device in zram_devices:
            pathname = f'/sys/class/block/{device}/mm_stat'
            if not os.path.exists(pathname):
                continue # not active
            with open(pathname, encoding='utf-8') as fh:
                ns = SimpleNamespace()
                for line in fh: # all the goodies are on 1st line
                    nums = line.split()
                    for idx, field in enumerate(fields):
                        setattr(ns, field, int(nums[idx]))
                    break
                infos[device] = ns
            for param in ('disksize', ):
                pathname = f'/sys/class/block/{device}/{param}'
                with open(pathname, encoding='utf-8') as fh:
                    for line in fh: # one value, one line
                        setattr(ns, param, int(line.strip()))
                        break
            if self.DB: print(f'DB: {device}: {ns}')

        self.devs = infos
        return infos

    def compute_effective(self, meminfoKB):
        """ Compute the effective values - now uses portable function """
        def seed(meminfoKB):
            nonlocal self
            meminfo = self.meminfo = SimpleNamespace()
            for key, value in meminfoKB.items():
                setattr(meminfo, key, value*1024)
            # artificial stats
            meminfo.MemUsed = meminfo.MemTotal - meminfo.MemAvailable
            meminfo.MemZram=0
            self.e_total = meminfo.MemTotal
            self.e_avail = meminfo.MemAvailable
            self.e_used = self.e_total - self.e_avail
            self.e_max_used = self.e_used
            return meminfo

        meminfo = seed(meminfoKB)
        devs = self._get_zram_stats()
        if not devs:
            return

        # Aggregate stats from all zRAM devices
        stats = None
        for stat in devs.values():
            # NOTE: generalization for more than one weak unless all
            #        are identically configured
            if not stats:
                stats = vars(stat).copy()
                continue
            for key, value in vars(stat).items():
                stats[key] += value
        stats = SimpleNamespace(**stats)
        self.meminfo.MemZram = stats.mem_used_total

        # Call the portable computation function
        result = compute_zram_effective(
            meminfo_total=meminfo.MemTotal,
            meminfo_used=meminfo.MemUsed,
            meminfo_available=meminfo.MemAvailable,
            zram_orig_data_size=stats.orig_data_size,
            zram_mem_used_total=stats.mem_used_total,
            zram_disksize=stats.disksize,
            zram_mem_limit=stats.mem_limit,
            limit_pct=self.limit_pct
        )

        # Update instance variables from result
        self.e_used = result.e_used
        self.e_avail = result.e_avail
        self.e_max_used = result.e_max_used
        self.ratio = result.ratio_current
        self.ratio_projected = result.ratio_projected
        self.projection_confidence = result.projection_confidence


####################################################################################
###### ProcMem class
####################################################################################
class ProcMem:
    """Represents the memory map summation for processes and groups.
      - the ProcMem object represents one process (or pid)
      - the ProcMem static data represents aggregate data for groups.
    """
    # pylint: disable=too-many-instance-attributes
    section_pat = re.compile(
            r'^([0-9a-f]+)-([0-9a-f]+)' # $1,$2: 00400000-004b8000
            + r'\s+([a-z-]+)'   # $3: r-xp
            + r'\s+([0-9a-f]+)'  # $4: 00000000
            + r'\s+(\S+)'  # $5: fd:00
            + r'\s+(\d+)'  # $6: 11143998
            + r'(\s*|\s+(\S.*))$' # $8: /.../inetrep
            , re.IGNORECASE)
    item_pat = re.compile(
            r'^(\w+):' # $1: MMUPageSize:
            + r'\s+(\d+)'  # $2: 4
            + r'\s+kb$'  # kB
            , re.IGNORECASE)
    junk_pat = re.compile(
            r'^(THPeligible|VmFlags|ProtectionKey)'
            , re.IGNORECASE)
    opts = None
    # debug = 0
    # summaries = {} # indexed by pid TODO remove this (replace by groups)
    # prcs = {}
    # groups = {} # indexed by group key (e.g., cmd)
    # divisor = 0 # determined by arguments
    # units = '' # determined by arguments
    # fwidth = 11
    pmemstat = None # the main program object
    max_cmd_len = 96 # command line maximum length
    chunk_dict = {
            'cat': None,
            'beg': 0,
            'end': 0,
            'offset': 0,
            'size': 0,
            'eSize': 0,
            'rss': 0,
            'pss': 0,
            'shared': 0,
            'private': 0,
            'swap': 0,
            'pswap': 0,
            'perms': '',
            'item': '',
            }
    clock_tick = None
    parse_err_cnt = 0

    def __init__(self, pid):
        self.pid = pid
        self.alive = True
        self.is_new = True
        self.wanted = True # until proven otherwise
        self.kernel = False # until proven otherwise
        self.is_changed = False
        self.why_not = None # populate me with why unwanted
        self.smaps_file = f'/proc/{self.pid}/smaps'
        self.rollup_file = f'/proc/{self.pid}/smaps_rollup'
        self.cpu = None
        self.exebasename = None, None
        self.key, self.cmdline, self.cmdline_trunc = None, None, None
        self.cgroup_path = None # cached cgroup v2 path (when grouping by cgroup)
        self.cgroup_read = False # whether the cgroup path has been read yet
        self.view_keys = None   # {grouping: key} for every supported view
        self.rollup = None      # last rollup summary (for view growth tracking)

    def refresh_cpu(self):
        """Get the Cpu Number for the PID (if possible)"""
        if not self.cpu:
            self.cpu = CpuSmooth(self.pid, avg_secs=getattr(
                ProcMem.opts, 'cpu_avg_secs', 20))
        return self.cpu.refresh_cpu() # sets self.cpu.percent

    def _get_exebasename(self, exepath, wds):
        """
        Final, definitive, robust executable base name resolver.
        """

        # --- Phase 1: Determine the Initial Best Name Candidate ---
        # ... (same initial logic as before, using os.readlink)
        basename = os.path.basename(exepath)
        is_bad_name = (not basename or basename == 'exe' or len(basename) > 16
                       or not re.search(r'[a-zA-Z]', basename))

        if is_bad_name:
            try:
                link_target = os.readlink(f'/proc/{self.pid}/exe')
                if ' (deleted)' not in link_target and 'a.out' not in link_target:
                    basename = os.path.basename(link_target)
            except (FileNotFoundError, OSError):
                pass

        # --- Phase 2: Multi-Step Aggressive Cleanup (The FINAL Final Fix) ---

        # 1. Remove VQ... style hashes and generic suffixes
        hash_and_bin_pattern = r'(VQ[A-Z0-9]{10,}|-bin|\.bin)'
        basename = re.sub(hash_and_bin_pattern, '', basename).strip()

        # 2. **CRITICAL FIX:** Remove all tab/PID/browser-related arguments from the tail.
        # This addresses both 'browser 14 tab' AND any simple trailing numbers (like 'browser 4')
        tab_and_num_pattern = r'(?:\s+\d+)?\s+(tab.*|socket.*|rdd.*|process.*|\d+)$'
        basename = re.sub(tab_and_num_pattern, '', basename, flags=re.IGNORECASE).strip()

        # 3. Remove common internal browser-role suffixes (renderer, rdd, content, etc.)
        roles_pattern = r'-(renderer|gpu|utility|plugin|content|chrm|bsp|extension)$'
        basename = re.sub(roles_pattern, '', basename, flags=re.IGNORECASE).strip()

        # Final safety cleanup for any leading/trailing punctuation/spaces leftover
        basename = re.sub(r'^\W+|\W+$', '', basename).strip()

        # --- Phase 3: Last Resort Fallback ---

        if not basename or basename in ('exe', 'a.out'):
            try:
                basename = os.path.basename(wds[0])
            except IndexError:
                basename = 'exe'

        # Special Case Protection: Restore I3 if it was reduced
        if basename == 'i':
            basename = 'i3'

        return basename

    def get_cmdline(self):
        """Get the command line of the PID."""

        try:
            cmdline_file = f'/proc/{self.pid}/cmdline'
            try:
                # Closed explicitly: this runs once per PID per scan, so a leaked
                # handle here would accumulate for the life of the process.
                with open(cmdline_file, encoding='utf-8') as handle:
                    line = handle.read()[:-1]
            except FileNotFoundError as exc:
                if DebugLevel:
                    DB(1, f'skip pid={self.pid} no-rollup-lines exc={type(exc).__name__}')
                return

            arguments = line.split('\0')
            if not arguments or not arguments[0]: # kernel process
                self.wanted, self.kernel = False, True
                return

            exepath = arguments[0]

            # Prepare wds (words/arguments) for use in the helper and cmdline rebuild
            base_name_list = os.path.basename(exepath).split()
            if base_name_list:
                wds = base_name_list[1:] + arguments[1:]
            else:
                wds = arguments[1:]

            # Use the robust helper to determine the final, clean basename
            self.exebasename = self._get_exebasename(exepath, wds)

            # --- START ELABORATION LOGIC ---

            # 1. Elaboration: The Sudo Wrapper
            if self.exebasename == 'sudo' and wds:
                actual_cmd_name = os.path.basename(wds[0])
                if actual_cmd_name:
                    self.exebasename = actual_cmd_name
                    del wds[0]

            # 2. Elaboration: The Interpreter (Python, Perl, etc.)
            if self.exebasename in ('python', 'python2', 'python3', 'perl', 'bash', 'ruby',
                    'sh', 'ksh', 'zsh') and wds:

                # 2.a. Catch the 'python -m <module>' pattern (The New Fix)
                if len(wds) >= 2 and wds[0] == '-m':
                    module_name = wds[1]
                    self.exebasename = f'{self.exebasename}->{module_name}'
                    # Remove '-m' and the module name from arguments
                    del wds[0]
                    del wds[0]

                # 2.b. Catch the standard '<script.py>' pattern (Original Logic)
                elif wds[0] and os.path.exists(wds[0]) and os.path.isfile(wds[0]):
                    script = os.path.basename(wds[0])
                    if script != wds[0]:
                        # Only elaborate if it's a clear script name, not a generic main
                        if not re.search(r'^__main__\.', script):
                            self.exebasename = f'{self.exebasename}->{script}'
                            del wds[0] # Remove the script path from arguments

            # --- END ELABORATION LOGIC ---

            self.cmdline = ' '.join([self.exebasename] + wds)
            self.cmdline_trunc = self.cmdline[0:ProcMem.max_cmd_len]

        except Exception as exc:
            # ... (rest of exception handling is the same)
            print(f'  WARNING: skip pid={self.pid} no-basename exc={exc}')
            print(traceback.format_exc())
            self.wanted = self.kernel = False
            self.why_not = 'CannotGetCmdline'
            return

        # filter unwanted before too much work
        if (ProcMem.opts.pids and str(self.pid) not in ProcMem.opts.pids
                and self.exebasename not in ProcMem.opts.pids):
            self.wanted = False
            self.why_not = 'FilteredByArgs'

        self.set_key()

    def read_cgroup(self):
        """Read and cache this process's cgroup v2 path (``None`` if absent)."""
        if self.cgroup_read:
            return self.cgroup_path
        self.cgroup_read = True
        try:
            with open(f'/proc/{self.pid}/cgroup', encoding='utf-8') as fhandle:
                lines = fhandle.read().splitlines()
        except OSError as exc:
            if DebugLevel:
                DB(3, f'pid={self.pid} no-cgroup exc={type(exc).__name__}')
            return None
        self.cgroup_path = parse_cgroup_lines(lines)
        return self.cgroup_path

    def set_key(self):
        """Compute the grouping key for this process, for every view.

        ``self.key`` is the key for the *active* grouping; ``self.view_keys``
        carries the key for every supported grouping so that growth history can
        be tracked for all of them at once (see :meth:`PmemStat.update_growth`),
        which keeps the leak baseline across a change of grouping.
        """
        cgroup = self.read_cgroup() or '(no-cgroup)'
        self.view_keys = {
            'exe': self.exebasename,
            'cmd': self.cmdline_trunc,
            'pid': self.pid,
            'cgroup': cgroup,
            'cgroupCharge': cgroup,
        }
        self.key = self.view_keys[ProcMem.opts.groupby]

    def read_lines(self, filename):
        """ Get the lines of the smaps """
        lines = None
        try:
            with open(filename, encoding='utf-8') as fhandle:
                lines = fhandle.read().splitlines()
        except (PermissionError, FileNotFoundError) as exc:
            # normal cases: not permitted or this is a race where the pid is terminating
            self.why_not = f'CannotReadLines({type(exc).__name__})'
        except Exception as exc:
            # unexpected cases (probably a bug)
            if not self.opts.window:
                print(f'ERROR: skip pid={self.pid}',
                      f'no-smaps-or-rollup-lines exc={type(exc).__name__}')
            self.why_not = f'CannotReadLines({type(exc).__name__})'
        return lines

    def get_rollup_lines(self):
        """Get the lines of the 'smaps_rollup' file for this PID"""
        rollup_lines = []
        try:
            rollup_lines = self.read_lines(self.rollup_file)
        except Exception as exc:
            rollup_lines = []
            if DebugLevel:
                DB(1, f'skip pid={self.pid} no-rollup-lines exc={type(exc).__name__}')

        if not rollup_lines:
            self.wanted = False
            self.why_not = 'CannotReadRollups'
        elif DebugLevel:
            DB(3, f'pid={self.pid} {self.exebasename} #rollup_lines={len(rollup_lines)}')

        return rollup_lines

    def get_smaps_lines(self):
        """Get the lines of the 'smaps' file for this PID"""
        smaps_lines = []
        try:
            smaps_lines = self.read_lines(self.smaps_file)
        except Exception as exc:
            smaps_lines = []
            if DebugLevel:
                DB(1, f'skip pid={self.pid} no-smap-lines exc={type(exc).__name__}')

        if not smaps_lines:
            self.wanted = False
            self.why_not = 'CannotReadSmaps'
        else:
            if DebugLevel:
                DB(1, f'pid={self.pid} {self.exebasename} #smaps_lines={len(smaps_lines)}')
        return smaps_lines

    def make_chunks(self, lines):
        """ Parse the already smaps read lines."""
        chunks = []
        chunk = None
        for idx, line in enumerate(lines):
            match = self.section_pat.match(line)
            if match:
                if chunk:
                    chunks.append(chunk)
                chunk = SimpleNamespace(**ProcMem.chunk_dict)
                chunk.beg = int(match.group(1), 16)
                chunk.end = int(match.group(2), 16)
                chunk.perms = match.group(3)
                chunk.offset = int(match.group(4), 16)
                chunk.item = match.group(8)
                continue
            match = self.item_pat.match(line)
            if match:
                tag = match.group(1)
                val = int(match.group(2))
                if tag == 'Size':
                    chunk.size = val
                elif tag == 'Rss':
                    chunk.rss = val
                elif tag.startswith('Shared'):
                    chunk.shared += val
                elif tag.startswith('Private'):
                    chunk.private += val
                elif tag == 'Swap':
                    chunk.swap = val
                elif tag == 'Pss':
                    chunk.pss = val
                continue
            match = self.junk_pat.match(line)
            if match:
                continue
            if not self.parse_err_cnt:
                print(f'ERROR: cannot parse "{line}" [{self.smaps_file}:{idx+1}]')
            self.parse_err_cnt += 1
        if chunk:
            chunks.append(chunk)
        return chunks

    @staticmethod
    def make_summary_dict(pid=0, info=''):
        """ Make an object to summarize memory use of a PID or group """
        summary = {
                'cpu%': 0,
                'memPSI%': 0,  # cgroup v2 memory.pressure some avg10 (%)
                'pswap': 0,
                'shSYSV': 0,
                'shOth': 0, # e.g., memory mapped file
                'stack': 0,
                'text': 0,
                'data': 0, # deprecated 'pseudo' (e.g., memory barrier) now in 'data'
                'ptotal': 0,
                # cgroup v2 kernel-charge slices (see README "Grouping by cgroup
                # v2"). anon/cache/kmem/swap partition the charge, oK is the
                # remainder (total - slices, normally just sock) so the row adds
                # up, and kcharge/footprt are the two view totals.
                'anon': 0,     # cgroup v2 anon (KB)
                'cache': 0,    # cgroup v2 page cache (KB): file or file-inactive_file
                'kmem': 0,     # cgroup v2 kernel (KB): kernel or kernel-slab_reclaimable
                'swap': 0,     # cgroup v2 kernel swap charge (KB), footprt view
                'oK': 0,       # cgroup v2 remainder (KB): total - slices
                'kcharge': 0,  # cgroup v2 memory.current (KB)
                'footprt': 0,  # cgroup v2 derived footprint (KB): current - reclaimable + swap
                'pss': 0,  # comes from rollups
                'number': -pid if pid else 0, # count if positive; else -pid
                'info': info,
                }
        return summary

    def parse_rollups(self, lines):
        """ Parse the already read lines."""
        summary = ProcMem.make_summary_dict()
        for idx, line in enumerate(lines):
            if not line.endswith('kB'):
                continue
            match = self.item_pat.match(line)
            if match:
                tag = match.group(1)
                val = int(match.group(2))
                if tag == 'Pss_Anon':
                    summary['data'] += val
                    summary['ptotal'] += val
                elif tag == 'Pss_File':
                    summary['text'] += val
                    summary['ptotal'] += val
                elif tag == 'Pss_Shmem':
                    summary['shOth'] += val
                    summary['ptotal'] += val
                elif tag == 'SwapPss':
                    summary['pswap'] += val
                    if self.pmemstat.has_zram():
                        summary['ptotal'] += val
                continue
            print(f'ERROR: cannot parse "{line}" [{self.rollup_file}:{idx+1}]')
        summary['pss'] = summary['ptotal'] # for consistency
        return summary

    def categorize_chunks(self, chunks):
        """ Analyze the chunks to categorize the memory """
        for idx, chunk in enumerate(chunks):
            chunk.eSize = chunk.size
            if chunk.cat: # if already done, don't do again
                continue

            if 's' in chunk.perms:
                if 'SYSV' in chunk.item:
                    chunk.cat = 'shSYSV'
                    # chunk.eSize = chunk.rss + chunk.swap
                    chunk.eSize = chunk.pss
                else:
                    chunk.cat = 'shOth'
                    chunk.eSize = chunk.pss
            elif chunk.item and '[stack]' in chunk.item:
                chunk.cat = 'stack'
                chunk.eSize = chunk.private
            elif (chunk.size == 4 and idx < len(chunks) - 1
                    and chunk.offset == chunk.beg and not chunk.item
                    and '---p' in chunk.perms):
                    # stack seems to be 4K unwriteable immediately followed
                    # by something very huge like 10240 or 10236.
                    # The size is bogus ... replace the 'Size' with
                    # the 'Private' plus swapped
                nchunk = chunks[idx+1]
                if (chunk.end == nchunk.end
                        and 'w' in nchunk.perms
                        and not nchunk.item
                        and nchunk.offset == nchunk.beg
                        and nchunk.size >= 10000
                        and nchunk.size <= 20000):
                    chunk.eSize = 0
                    chunk.cat = 'data' # was 'pseudo'
                    nchunk.eSize = nchunk.private + nchunk.swap
                    nchunk.cat = 'stack'
            if not chunk.cat:
                if '---' in chunk.perms:
                    chunk.cat = 'data' # was 'pseudo'
                    chunk.eSize = 0
                elif 'w' in chunk.perms:
                    chunk.cat = 'data'
                    chunk.eSize = chunk.rss + chunk.swap
                else:
                    chunk.cat = 'text'
                    chunk.eSize = chunk.pss + chunk.swap
        if DebugLevel:
            for chunk in chunks:
                DB(6, '{self.pid} {self.exebasename} CHUNK:', chunk)

    def summarize_chunks(self, chunks):
        """ Accumulate the chunks into the summary of memory use for the PID """
        summary = self.make_summary_dict(self.pid)

        for chunk in chunks:
            if DebugLevel:
                DB(5, f'{self.pid} {self.exebasename} BLK: {chunk.cat} eSize={chunk.eSize}'
                    + f' size={chunk.size} {chunk.perms} {chunk.item}')
            summary[chunk.cat] += chunk.eSize
            summary['ptotal'] += chunk.eSize
            summary['pswap'] += chunk.pswap
        # print(f'DB self.summaries[{key}]: {self.summaries[key]}')
        return summary

    def prc_pid(self):
        """Process one PID"""
        self.alive = True
        self.is_changed = False
        self.rollup = None
        if not self.why_not and not self.cmdline:
            self.get_cmdline()
            if not self.cmdline:
                return
        rollup_lines = []
        if not self.why_not:
            rollup_lines = self.get_rollup_lines()
        if self.why_not:
            DB(4, f'pid={self.pid} {self.exebasename} why_not={self.why_not}')
            return
        self.is_changed = False
        rollup_summary = self.parse_rollups(rollup_lines)
        self.rollup = rollup_summary
        if self.opts.cpu:
            rollup_summary['cpu%'] = self.cpu.percent
        group = self.pmemstat.get_group(self.key)
        if not group.alive:
            info = str(self.key)
            if ProcMem.opts.groupby == 'pid':
                info += ' ' + self.cmdline_trunc
            group.rollup_summary = ProcMem.make_summary_dict(info=info)
            group.summary = ProcMem.make_summary_dict(info=info)
            group.alive = True
        self.pmemstat.add_to_summary(rollup_summary, group.rollup_summary)
        group.prcset.add(self)

######
####################################################################################
######

class PmemStat:
    """ The singleton class for running the main loop, etc"""

    # Always-visible "evidence" of the available keys, shown as the top header
    # line so the options are discoverable without opening the help screen.
    # Keep this in sync with the OptionSpinner registrations in window_loop().
    # '?' is deliberately first: if the terminal is too narrow to render the
    # whole line, only the least-critical trailing entries are truncated while
    # the gateway to the complete list (and the navigation keys) stays visible.
    KEY_LEGEND = ('[?]:help [g]roup [u]nits [s]ort [c]pu [K]ill [P]SI'
                  ' [G]rowth')
    # Appended to the legend only when running as root *and* the growth
    # annotation is showing: 'z' zaps (resets) the cross-run growth history,
    # which is only meaningful with root's complete process view (the ledger's
    # coverage depends on it) and only visible when growth is displayed.  See
    # zap_history() and zap_key_offered().
    KEY_LEGEND_ZAP = ' [z]ap'
    # Shown in place of the legend line's leading pad when running degraded
    # (not root): a non-root run omits other users' processes, so the first
    # header line is stamped with the '--user' flag that selects this mode. It
    # shares the legend's dim attribute so it reads as status, not as a key.
    KEY_LEGEND_USER = '--user'
    # PSS categories folded into the single combined "other" column when -o is
    # not given; the first entry is the rendered slot (the sum is shown there).
    OTHER_KEYS = ('shSYSV', 'shOth', 'stack', 'text')
    # Header label for the inline growth column (the field in front of the
    # {num}x count). Its length is the column's minimum width whenever growth
    # is enabled, so the label always fits and the header/rows stay aligned
    # even while no row is currently annotating.
    GROWTH_HEADER = 'growth'
    # Grow a width immediately; shrink it only after this many consecutive
    # smaller refreshes (hysteresis against frame-to-frame jitter).
    WIDTH_SHRINK_DELAY = 4
    # Below this interval the per-day rate is a wild extrapolation (a few
    # seconds projected to a day), so rate mode shows nothing until then.
    RATE_MIN_SECS = 60
    # A tracker not fed for this long is dropped. This bounds memory as pids
    # churn in the 'pid' view; a key absent for an hour has no leak history
    # worth resuming.
    GROWTH_PRUNE_SECS = 3600
    # Pressing 'z' never resets anything by itself: it opens a confirmation
    # prompt that must be answered within this many seconds.  Resetting the
    # growth baseline throws away every number the 'G'rowth view has built up,
    # so no answer -- or a stray key -- must never do it.  ESC also declines.
    ZAP_CONFIRM_SECS = 10.0
    # The question shown while the 'z' reset awaits an answer.
    ZAP_CONFIRM_TEXT = ('⚠️  Really want to reset the growth baseline to now?  '
                        'Enter "y" or ESC')

    def __init__(self, opts):
        self.opts = opts
        self.loop_num = 0
        self.debug = opts.debug
        self.prcs = {}
        self.kernel_prcs = []
        self.groups = {} # indexed by group key (e.g., cmd)
        self.window = None
        self.vmstat = None
        self.pressure = {}  # PSI snapshot (only read when opts.psi)
        self.spin = OptionSpinner()
        self.units, self.divisor, self.fwidth = 0, 0, 0
        setattr(opts, 'kill_mode', False) # pseudo option
        # Screen-model state.  The report's window model survives between
        # frames, so `needs_refresh` is what asks for a fresh /proc sample (a
        # timeout, a presentation key, or returning from another screen), and
        # `regroup_pending` carries the mode/fit change into that sample.
        self.screens = {}
        self.stack = None
        self.prefs = getattr(opts, 'prefs', None)
        self.needs_refresh = True
        self.first_loop = True
        self.regroup_pending = True
        set_colors_enabled(getattr(opts, 'colors', True))
        self.groups_by_line = {}
        # Persistent per-(view, key) growth trackers. They live on the singleton
        # rather than on the groups, so changing the grouping (which rebuilds
        # self.groups) does not discard the leak history; update_growth feeds
        # every view each loop.
        self.growth_trackers = {}
        self.growth_results = {}
        self.cgroup_keys = set()
        self.cgroup_raw = {}            # cgroup key -> raw (hierarchical) values
        self.cgroup_data_seen = False   # any group had readable cgroup v2 data
        self.cgroup_charge_keys = set()  # cgroups with a computable charge total
        self.lead_width = 1             # width of the growth column
        self.sys_growth_width = 1       # width of the system line values
        self._lead_small = 0            # consecutive smaller-refresh counts
        self._sysgrow_small = 0
        self._last_growth_mode = None
        # System-wide growth trackers (fed each loop; see update_growth).
        self.sys_trackers = {key: GrowthTracker() for key in (
            'Used', 'TOTALS', 'ShTmp', 'Oth', 'OthK', 'OthU')}
        self.sys_growth = {}
        # Cross-run growth ledger.  The boot id is captured once (it is stable
        # for the life of the process); growth_samples is refreshed every loop by
        # update_growth so the periodic ledger write is cheap.  history_use seeds
        # the in-memory trackers from the ledger's baselines; history_save writes
        # the ledger.  PMEMSTAT_NO_HISTORY disables both; the CLI flags disable
        # one each.
        no_history = bool(os.environ.get(HISTORY_ENV_DISABLE))
        self.history_use = not no_history and not getattr(
            opts, 'no_growth_history', False)
        self.history_save = not no_history and not getattr(
            opts, 'dont_save_growth_history', False)
        # The ledger holds system-wide samples, which only root can produce (and
        # only a root run's coverage is a consistent sample space across runs).
        # Non-root sees just one user's processes, so seeding from a
        # root-written baseline -- or writing a user-only baseline into the same
        # per-user ledger for a later root run to trust -- would mix two
        # incompatible sample spaces.  Non-root therefore ignores the ledger
        # entirely and measures growth from this run's start only (the same
        # behaviour as --no-growth-history).
        if os.geteuid() != 0:
            self.history_use = False
            self.history_save = False
        self.history_boot = History.current_boot()
        self.update_secs = History.update_secs()
        self.growth_samples = {}
        self.sys_samples = {}
        self._history_seeded = False
        self._history_saved_once = False
        self._last_ledger_save = 0.0
        self._history_finalized = False
        self._set_units()
        self.zram_projector = ZramProjector()
        # Initialize inline search bar
        self.search_bar = IncrementalSearchBar(
            on_change=lambda text: setattr(self.opts, 'search', text),
            on_accept=lambda text: self._search_accept(),
            on_cancel=self._search_cancel
        )
        # Initialize inline confirmation for kill operations
        self.confirmation = InlineConfirmation()
        # Deadline (monotonic) of the armed 'z' reset prompt, else None.  See
        # zap_confirm_start(): 'z' only asks, 'y' resets, ESC / no answer does
        # not.
        self.zap_confirm_deadline = None

    @property
    def win(self):
        """Alias for :attr:`window`, the name console-window's Screen expects.

        ``Screen.__init__`` stores ``app.win``; pmemstat has always called the
        window ``window``, so the alias keeps one name without losing the other.
        """
        return self.window

    def has_zram(self):
        """Have zRAM actual? """
        return bool(self.zram_projector and self.zram_projector.devs)

    def _search_accept(self):
        """Called when search is accepted (Enter pressed)"""
        if self.window:
            self.window.passthrough_mode = False

    def _search_cancel(self, original_text):
        """Called when search is cancelled (Esc pressed)"""
        self.opts.search = original_text
        if self.window:
            self.window.passthrough_mode = False

    def get_sortby(self):
        """Make sort_by sensible."""
        if self.opts.sortby in ('cpu',) and not self.opts.cpu:
            return 'mem'
        return self.opts.sortby

    def is_fit_opted(self):
        """Make fit_to_window sensible."""
        return self.opts.fit_to_window and self.get_sortby() in (
            'mem', 'cpu')

    def _set_units(self):
        self.units = self.opts.units
        if self.units == 'MB':
            self.divisor = 1024*1024
            self.fwidth = 8
        elif self.units == 'KB':
            self.divisor = 1024 # KB (the original)
            self.fwidth = 11
        else: # human
            self.divisor = 1 # human
            self.fwidth = 7
        # Right-aligned header labels touch when a label is as wide as the
        # column, so keep the width at least one wider than the longest label
        # (e.g. "kmem"+"footprt", or "cpu%"+"memPSI%" at human width).
        label_width = max(len(name) for name in ProcMem.make_summary_dict())
        self.fwidth = max(self.fwidth, label_width + 1)

    def get_group(self, key):
        """Per group info."""
        group = self.groups.get(key, None)
        if not group:
            group = SimpleNamespace(key=key,
                    is_new=True,
                    alive=False,
                    why_not=None,
                    is_changed=False,
                    o_prcset=set(),
                    prcset=set(),
                    o_rollup_summary=None,
                    rollup_summary=None,
                    o_summary=None,
                    summary=None,
                    first_summary=None,
                    growth_pct=0.0,
                    growth_val=None,
                    growth_interval=0.0,
                    growth_rate=None,
                    growth_txt='')
            self.groups[key] = group
            # DB(0, f'add group[{key}]')
        return group

    def cgroup_label(self, group):
        """Display label for a cgroup row.

        Usually the cgroup leaf unit name (with a trailing '+' when it has
        listed descendants). But systemd app scopes are often named after the
        launcher that spawned the app (e.g. an ``app-niri-fuzzel-*.scope`` that
        actually hosts VS Code), so when the leaf name matches none of the
        member executables the row is labelled with the dominant member app
        instead. The full cgroup path stays searchable (see the report loop).
        """
        label = cgroup_leaf(group.key)
        exes = {}
        for prc in group.prcset:
            name = prc.exebasename
            if name:
                exes[name] = exes.get(name, 0) + 1
        if exes:
            leaf = label.lower().rstrip('+')

            def matches(name):
                low = name.lower()
                if low in leaf:
                    return True
                tokens = re.findall(r'[a-z0-9]+', low)
                return bool(tokens) and len(tokens[0]) >= 3 and tokens[0] in leaf

            if not any(matches(name) for name in exes):
                label = max(exes.items(), key=lambda kv: (kv[1], kv[0]))[0]
        if isinstance(group.key, str) and has_descendant(
                self.cgroup_keys, group.key):
            label += '+'
        return label

    def update_cgroup_summary(self, group):
        """Read the kernel's cgroup v2 numbers for a group (raw/subtree).

        The label is the leaf unit name, marked with a trailing "+" when the
        cgroup also contains descendant cgroups that are listed separately.

        The numeric columns are a decomposition of the kernel's own per-cgroup
        charge, each slice taken from ``memory.stat`` so the row reconciles to
        the view total (``memory.current`` for the kcharge view, or the derived
        ``footprt`` for the footprt view):

          * ``anon``  = ``anon``: anonymous memory charged to the cgroup.
          * ``cache`` = ``file`` (kcharge) or ``file - inactive_file``
            (footprt): page cache; reclaimable inactive cache is removed in the
            footprt view.
          * ``kmem``  = ``kernel`` (kcharge) or ``kernel - slab_reclaimable``
            (footprt): kernel slabs + stacks + pagetables.
          * ``swap``  = the kernel swap charge (footprt view only; swap is not
            part of ``memory.current``).
          * ``oK``    = total - the slices above (normally just ``sock``), so
            the row adds up exactly.

        The values read here are *raw* (they include descendant cgroups); they
        are cached in ``self.cgroup_raw`` and :meth:`apply_cgroup_locals`
        reduces them to each row's own share so the rows sum to TOTALS. Memory
        values are stored in KB (like every other column) so ``-u`` applies;
        ``memPSI%`` is a percentage, handled like ``cpu%``.
        """
        group.summary['info'] = self.cgroup_label(group)
        # Reset the cgroup columns (the summary may be a reused object); the
        # memory columns are filled by apply_cgroup_locals().
        for key in ('anon', 'cache', 'kmem', 'swap', 'oK'):
            group.summary[key] = 0
        group.summary['kcharge'] = None
        group.summary['footprt'] = None
        group.summary['memPSI%'] = 0
        if not isinstance(group.key, str):
            return
        data = CGroup(group.key).read()
        stat = data['stat'] or {}
        raw = {'anon': 0, 'cache': 0, 'kmem': 0, 'swap': 0,
               'kcharge': 0, 'footprt': 0}
        totals = cgroup_view_totals(data)
        if totals is not None:
            self.cgroup_data_seen = True
            swap = stat.get('swap')
            if swap is None:
                swap = data.get('swap_current') or 0
            inactive_file = stat.get('inactive_file', 0)
            slab_reclaimable = stat.get('slab_reclaimable', 0)
            raw['anon'] = stat.get('anon', 0) // 1024
            # Record *both* totals, not just the active view's, so the growth
            # loop can reuse this single read for both cgroup groupings.
            raw['footprt'] = totals['footprt']
            raw['kcharge'] = totals['kcharge']
            if self.cgroup_view() == 'footprt':
                raw['cache'] = (stat.get('file', 0) - inactive_file) // 1024
                raw['kmem'] = (stat.get('kernel', 0) - slab_reclaimable) // 1024
                raw['swap'] = swap // 1024
            else:
                raw['cache'] = stat.get('file', 0) // 1024
                raw['kmem'] = stat.get('kernel', 0) // 1024
            self.cgroup_charge_keys.add(group.key)
        self.cgroup_raw[group.key] = raw
        some = (data['pressure'] or {}).get('some') or {}
        if 'avg10' in some:
            group.summary['memPSI%'] = some['avg10']

    def apply_cgroup_locals(self):
        """Reduce each group's cgroup columns to its own ("local") share.

        cgroup v2 accounting is hierarchical, so a parent's raw numbers include
        its descendant groups'. Subtracting the descendants' raw values makes
        every row its own share and lets the rows sum to TOTALS; the "+" marker
        flags the rows that had descendants subtracted. ``oK`` is recomputed on
        the local slices so the row still reconciles to the (local) total.
        """
        cols = ('anon', 'cache', 'kmem', 'swap', 'kcharge', 'footprt')
        locals_by_key = local_values(self.cgroup_raw, cols)
        total_key = self.total_key()
        for group in self.groups.values():
            if not (group.alive and group.summary):
                continue
            values = locals_by_key.get(group.key)
            if not values:
                continue
            if group.key not in self.cgroup_charge_keys:
                for key in ('anon', 'cache', 'kmem', 'swap', 'oK',
                            'kcharge', 'footprt'):
                    group.summary[key] = None
                continue
            group.summary.update(values)
            for key in cols:
                if group.summary.get(key) is not None:
                    group.summary[key] = max(0, group.summary[key])
            total = group.summary.get(total_key)
            parts = (group.summary['anon'] + group.summary['cache']
                     + group.summary['kmem'])
            if total_key == 'footprt':
                parts += group.summary['swap']
            group.summary['oK'] = (total - parts) if total is not None else None

    def prep_new_loop(self, regroup):
        """Prepare for a new loop.
        Returns whether or not any groups are left.
        If not, it will be time to terminate.
        """
        if regroup:
            self.groups = {}
        if self.groups:
            for key in list(self.groups):
                group = self.groups[key]
                if not group.alive:
                    del self.groups[key]
                    # DB(0, f'del group[{key}]')
                    continue
                group.is_new = False
                group.alive = False
                group.o_rollup_summary, group.rollup_summary = group.rollup_summary, None
                if group.prcset:
                    group.o_prcset, group.prcset = group.prcset, set()
                group.is_changed = False
                group.delta_pss = 0

        for pid in list(self.prcs):
            prc = self.prcs[pid]
            if regroup:
                prc.set_key()
            if not prc.alive:
                del self.prcs[pid]
                continue
            prc.alive = False
        return self.groups

    @staticmethod
    def add_to_summary(summary, total):
        """ Add a summary memory use into a running total of memory use """
        if summary and total:
            for key, val in summary.items():
                if key in ('info', 'memPSI%'):
                    pass
                elif key in ('number',):
                    total[key] += 1 if val <= 0 else val
                elif isinstance(val, (int, float)):
                    total[key] += val

    def growth_metric_value(self, summary):
        """Scalar used for loop filtering and growth ("total incl. swap").

        Under zRAM the rollup ``ptotal`` already contains ``SwapPss`` (see
        :meth:`ProcMem.parse_rollups`), so ``pswap`` must not be added again;
        without zRAM it must be. This keeps the metric correct in both cases
        and fixes a latent double count.
        """
        return summary['pss'] + (0 if self.has_zram() else summary['pswap'])

    def test_delta(self, group, summary, o_summary):
        """Check whether the group rollup or smaps summary exceeds threshold """
        # pylint: disable=chained-comparison
        is_over = False
        # DB(0, f'{group.key} o=[{group.o_summary}]\n          n=[{group.summary}]')
        delta_pss = (self.growth_metric_value(summary)
                     - self.growth_metric_value(o_summary))
        thresh = self.opts.min_delta_kb

        # DB(0, f'{group.key} ~pss {delta_pss}KB min={self.opts.min_delta_kb}')
        # DB(0, f'{group.key} ~pss {delta_pss}KB thresh={thresh}')
        if ((thresh <= 0 and abs(delta_pss) >= -thresh)
                or (thresh > 0 and delta_pss >= thresh)):
            is_over = True
            if self.debug:
                DB(2, f'{group.key} ~pss {delta_pss}KB thresh={thresh}')
        return is_over, delta_pss

    def prc_growth_metric(self, prc):
        """Per-process scalar fed to the non-cgroup view trackers (KB).

        Uses the rollup total including swap (zRAM-aware), i.e. the same metric
        the non-cgroup rows display.
        """
        return self.growth_metric_value(prc.rollup)

    def cgroup_growth_samples(self, samples):
        """Override the cgroup views' samples with kernel charge totals.

        Reuses the reads already done for an active cgroup grouping when
        possible; otherwise reads each distinct cgroup once (both cgroup
        groupings are fed from the single read). Values are reduced to each
        cgroup's own ("local") share so they match the displayed rows.
        """
        keys = set()
        for prc in self.prcs.values():
            if prc.wanted and not prc.why_not and prc.view_keys:
                key = prc.view_keys['cgroup']
                if key != '(no-cgroup)':
                    keys.add(key)
        if not keys:
            return
        if self.is_cgroup() and self.cgroup_charge_keys:
            raw = {key: {'footprt': self.cgroup_raw[key]['footprt'],
                         'kcharge': self.cgroup_raw[key]['kcharge']}
                   for key in self.cgroup_charge_keys}
        else:
            raw = {}
            for key in keys:
                totals = cgroup_view_totals(CGroup(key).read())
                if totals is not None:
                    raw[key] = totals
        for key, values in local_values(raw, ('footprt', 'kcharge')).items():
            samples['cgroup'][key] = max(0, values['footprt'])
            samples['cgroupCharge'][key] = max(0, values['kcharge'])

    def feed_growth(self, now, view, key, value):
        """Feed one ``(view, key)`` sample into its persistent tracker."""
        skey = (view, key)
        tracker = self.growth_trackers.get(skey)
        if tracker is None:
            tracker = GrowthTracker()
            self.growth_trackers[skey] = tracker
        tracker.last_seen = now
        return tracker.update(now, value)

    def prune_growth_trackers(self, now):
        """Drop trackers whose key has not been seen for GROWTH_PRUNE_SECS."""
        stale = [skey for skey, tracker in self.growth_trackers.items()
                 if tracker.last_seen is None
                 or now - tracker.last_seen > self.GROWTH_PRUNE_SECS]
        for skey in stale:
            del self.growth_trackers[skey]

    def update_growth(self, now, grand_summary, meminfoKB):
        """Feed every view's growth trackers a sample (history survives regroup).

        Trackers are keyed by ``(view, key)`` and live on the singleton, so the
        leak history is continuous from the start of the tool no matter how the
        grouping is switched. Every view is fed each loop, not just the active
        one: the non-cgroup views share a single pass over the per-process
        rollups (cheap), and the cgroup views share one read per distinct cgroup
        (reused from the cgroup view when it is active). Trackers run
        unconditionally (even when the growth annotation is ``off``) so data is
        ready the moment it is switched on.
        """
        samples = {view: {} for view in GROWTH_VIEWS}
        for prc in self.prcs.values():
            if (not prc.wanted or prc.why_not or not prc.view_keys
                    or prc.rollup is None):
                continue
            metric = self.prc_growth_metric(prc)
            for view, key in prc.view_keys.items():
                samples[view][key] = samples[view].get(key, 0) + metric
        self.cgroup_growth_samples(samples)
        # Keep the raw per-view samples so history can snapshot them cheaply.
        self.growth_samples = samples
        total = meminfoKB['MemTotal']
        avail = meminfoKB['MemAvailable']
        sh_tmp = meminfoKB['Shmem']
        othk = (meminfoKB['SUnreclaim'] + meminfoKB['KernelStack']
                + meminfoKB['PageTables'])
        oth = total - grand_summary['ptotal'] - avail - sh_tmp
        self.sys_samples = {
            'Used': total - avail,
            'TOTALS': grand_summary['ptotal'],
            'ShTmp': sh_tmp,
            'Oth': oth,
            'OthK': othk,
            'OthU': oth - othk,
        }
        # Seed the trackers from the ledger baselines exactly once per process,
        # after the samples exist but before any result is computed, so the very
        # first annotations already measure growth since the first observation.
        # The system metrics are seeded too, so the header deltas share that same
        # baseline and interval.
        if self.history_use and not self._history_seeded:
            self._history_seeded = True
            wall = datetime.now(timezone.utc)
            baselines = History.baselines(boot=self.history_boot)
            seed_from_baselines(self.growth_trackers, baselines, samples,
                                now, wall)
            sys_baselines = History.sys_baselines(boot=self.history_boot)
            seed_sys_from_baselines(self.sys_trackers, sys_baselines,
                                    self.sys_samples, now, wall)
        self.growth_results = {}
        for view, by_key in samples.items():
            self.growth_results[view] = {
                key: self.feed_growth(now, view, key, value)
                for key, value in by_key.items()}
        self.prune_growth_trackers(now)
        # Attach the active grouping's results to its group objects.
        results = self.growth_results.get(self.opts.groupby, {})
        for group in self.groups.values():
            if not group.alive:
                continue
            result = results.get(group.key)
            if result is None:
                group.growth_val = None
                group.growth_interval = 0.0
                group.growth_rate = None
            else:
                group.growth_val, group.growth_interval = result
                minutes = group.growth_interval / 60.0
                group.growth_rate = (group.growth_val / minutes
                                     if minutes > 0 else 0.0)
        self.sys_growth = {key: self.sys_trackers[key].update(now, value)
                           for key, value in self.sys_samples.items()}

    def history_begin(self):
        """Startup housekeeping: drop obsolete phase-1 anchor/live/marker files."""
        if not self.history_save and not self.history_use:
            return
        try:
            History.cleanup_obsolete()
        except OSError:
            pass

    def history_update(self, now=None, force=False):
        """Merge the latest samples into the ledger (throttled unless forced).

        Called on the first loop (to capture baselines promptly) and then on the
        ``History.update_secs()`` timer, reusing the monotonic throttle pattern.
        ``force`` writes regardless of the timer so the final flush on exit is
        never skipped.  Idempotent: merging the same samples is harmless.
        """
        if not self.history_save or not self.growth_samples:
            return
        now = time.monotonic() if now is None else now
        if (not force and self._history_saved_once
                and now - self._last_ledger_save < self.update_secs):
            return
        self._history_saved_once = True
        self._last_ledger_save = now
        try:
            History.update_ledger(self.growth_samples, self.sys_samples,
                                  boot=self.history_boot)
        except OSError:
            pass

    def history_finalize(self):
        """Write one final ledger update on the way out (idempotent)."""
        if self._history_finalized:
            return
        self._history_finalized = True
        self.history_update(force=True)

    def zap_history(self):
        """Runtime equivalent of ``--reset-history-now`` (the ``z`` key).

        Forgets every in-memory growth anchor and rewrites the ledger from the
        current samples, so all later growth annotations and deltas are measured
        from this moment -- exactly as if the tool had first run just now. The
        in-memory trackers are reset unconditionally (they are what the live
        display reads); the ledger write happens only when history saving is
        enabled, matching :func:`reset_history_now`.

        Root-only: the ledger's coverage depends on seeing every process, so the
        caller offers the key only when running as root. Returns ``True`` when
        the ledger was rewritten, ``False`` otherwise.
        """
        if os.geteuid() != 0:
            return False
        for tracker in self.growth_trackers.values():
            tracker.reset()
        for tracker in self.sys_trackers.values():
            tracker.reset()
        self.growth_results = {}
        self.sys_growth = {}
        # The ledger baselines are being replaced, so never re-seed the (now
        # fresh) in-memory trackers from a stale ledger.
        self._history_seeded = True
        if not (self.history_save and self.growth_samples):
            return False
        try:
            return History.reset_ledger(self.growth_samples, self.sys_samples,
                                        boot=self.history_boot)
        except OSError:
            return False

    def zap_confirm_active(self):
        """True while the ``z`` reset prompt is waiting for an answer."""
        return self.zap_confirm_deadline is not None

    def zap_confirm_remaining(self):
        """Seconds left to answer the ``z`` reset prompt (0.0 when inactive)."""
        if self.zap_confirm_deadline is None:
            return 0.0
        return max(0.0, self.zap_confirm_deadline - time.monotonic())

    def zap_confirm_start(self):
        """Arm the ``z`` confirmation; nothing is reset until it is answered.

        Resetting the baseline is destructive (every ``G``rowth number restarts
        from now), so the key only opens a prompt.  It closes by itself after
        :data:`ZAP_CONFIRM_SECS` with no answer, which counts as ``no``.
        """
        self.zap_confirm_deadline = time.monotonic() + self.ZAP_CONFIRM_SECS

    def zap_confirm_cancel(self):
        """Withdraw the ``z`` prompt without resetting anything."""
        self.zap_confirm_deadline = None

    def zap_confirm_text(self):
        """The prompt line shown while the ``z`` reset awaits an answer."""
        remaining = int(round(self.zap_confirm_remaining()))
        return f'{self.ZAP_CONFIRM_TEXT}  [{remaining}s]'

    def zap_confirm_handle_key(self, key):
        """Answer the ``z`` prompt: ``y`` confirms, ESC declines.

        Returns ``'confirmed'``, ``'cancelled'`` or ``'timeout'`` when the
        prompt closes, and ``'continue'`` while it stays open.  Any other key is
        deliberately ignored (the prompt keeps counting down), so a stray
        keystroke can neither confirm nor cancel the reset.
        """
        if not self.zap_confirm_active():
            return 'continue'
        if self.zap_confirm_remaining() <= 0:
            self.zap_confirm_cancel()
            return 'timeout'
        if key in (ord('y'), ord('Y')):
            self.zap_confirm_cancel()
            return 'confirmed'
        if key == 27:  # ESC
            self.zap_confirm_cancel()
            return 'cancelled'
        return 'continue'

    def handle_zap_confirm_key(self, key):
        """Feed ``key`` to the ``z`` prompt, resetting only on a ``y`` answer.

        Returns True when the prompt is (or was) active, i.e. the key belonged
        to it and must not reach the normal key handlers.
        """
        if not self.zap_confirm_active():
            return False
        result = self.zap_confirm_handle_key(key)
        if result == 'continue':
            return True
        if result == 'confirmed':
            msg = ('Growth history zapped' if self.zap_history()
                   else 'Growth baseline reset (ledger not rewritten)')
        elif result == 'timeout':
            msg = 'Growth baseline unchanged (timed out)'
        else:
            msg = 'Growth baseline unchanged'
        if self.window:
            self.window.flash(msg, duration=1.2)
        return True

    def prompt_secs(self, default):
        """Prompt timeout, shortened so an armed ``z`` prompt cannot outlive it."""
        if not self.zap_confirm_active():
            return default
        return min(default, max(0.01, self.zap_confirm_remaining()))

    def zap_confirm_expire(self):
        """Close an unanswered ``z`` prompt (its deadline passed while idle).

        Returns True when a prompt was withdrawn.  Called from the prompt
        timeout path, i.e. when no key arrived to answer it.
        """
        if not self.zap_confirm_active():
            return False
        if self.zap_confirm_remaining() > 0:
            return False
        self.zap_confirm_cancel()
        if self.window:
            self.window.flash('Growth baseline unchanged (timed out)',
                              duration=1.2)
        return True

    def growth_text(self, group):
        """Format a group's growth annotation for the current mode."""
        if group.growth_val is None:
            return ''
        mode = self.opts.growth_style
        if mode == 'growth':
            return human_kb(group.growth_val)
        if mode == 'rate':
            if group.growth_interval < self.RATE_MIN_SECS:
                return ''
            minutes = group.growth_interval / 60.0
            per_day = (group.growth_val * 1440.0 / minutes
                       if minutes > 0 else 0)
            return f'{human_kb(per_day)}/d'
        return f'{human_kb(group.growth_val)} {ago_str(group.growth_interval)}'

    def growth_lead_min_width(self):
        """Minimum width of the lead (growth) column.

        While the growth annotation is enabled the column must be at least as
        wide as its header label, so ``growth`` always fits above the field and
        the header tracks the rows' reserved space even when no row annotates.
        With growth ``off`` the column collapses to the single character the
        'A' (new group) marker needs.
        """
        if getattr(self.opts, 'growth_style', 'off') == 'off':
            return 1
        return len(self.GROWTH_HEADER)

    def growth_lead_header(self):
        """Header fragment for the lead (growth) column, including its pad.

        One leading space then the label right-justified in the column, which
        lines the label's last character up with the column's right edge, like
        every other column header.  With growth ``off`` the fragment is the
        same width of blank space (one space plus the column plus the separator)
        so ``key/info`` stays where the rows put it.

        This is emitted on the TOTALS row (the last header line, directly above
        the annotations it titles -- see :meth:`lead_segment`); the column
        header line above it reserves the same width with
        :meth:`growth_lead_blank`.
        """
        if getattr(self.opts, 'growth_style', 'off') == 'off':
            return ' ' * (self.lead_width + 1)
        return f' {self.GROWTH_HEADER:>{self.lead_width}}'

    def growth_lead_blank(self):
        """Blank lead field for the column-header line.

        The ``growth`` label rides on the TOTALS row instead, so the column
        header reserves the identical width with blank space: ``key/info`` does
        not move, and the only two lines that change are the two that carry the
        label and the marker.
        """
        return ' ' * (self.lead_width + 1)

    def select_growth_rows(self, alive_groups):
        """Group keys allowed to annotate: absolute ``-k`` gate plus top-N."""
        mode = self.opts.growth_style
        if mode == 'off':
            return set()
        thresh = self.opts.min_delta_kb
        candidates = []
        for key, group in alive_groups.items():
            growth = group.growth_val
            if growth is None:
                continue
            if not ((thresh <= 0 and abs(growth) >= -thresh)
                    or (thresh > 0 and growth >= thresh)):
                continue
            rank = (group.growth_rate if mode in ('both', 'rate')
                    else growth)
            candidates.append((rank if rank is not None else 0.0, key))
        limit = getattr(self.opts, 'growth_top_n', None)
        if limit is not None:
            candidates.sort(reverse=True)
            candidates = candidates[:limit]
        return {key for _, key in candidates}

    def _debounced_width(self, current, need, count):
        """Grow ``current`` to ``need`` at once; shrink after a delay.

        Returns ``(width, new_count)``. Shrinking waits for
        ``WIDTH_SHRINK_DELAY`` consecutive smaller refreshes so the column does
        not flap.
        """
        if need > current:
            return need, 0
        if need < current:
            count += 1
            if count >= self.WIDTH_SHRINK_DELAY:
                return need, 0
            return current, count
        return current, 0

    def format_sys_growth(self):
        """One line: system growth for Used/TOTALS/Sh+Tmp and the residual.

        As root (where every process is accounted for) the residual is split
        into ``OthK``/``OthU``; otherwise it is shown as a single ``Oth`` to
        match the leader line (see :meth:`oth_text`).  Values (non-negative, so
        no ``+``) are left-justified to a common width for the current mode.
        The shared baseline interval is shown once, as a trailing
        ``[interval]`` in every growth mode (``both``/``growth``/``rate``).
        The brackets make it clear that the age applies to all the deltas (all
        trackers share one baseline) rather than looking like an attribute of
        the last field, and it gives the context needed to judge the numbers --
        in particular a ``rate`` projection, which is otherwise silent about the
        window it extrapolates from.
        """
        return ''.join(text for text, _ in self.sys_growth_segments())

    def sys_growth_segments(self):
        """The system growth line as ``(text, attr)`` runs.

        Concatenating the runs reproduces :meth:`format_sys_growth` exactly, so
        the value width (and its shrink hysteresis) is unchanged; only the
        styling differs.  The ``Δ`` values carry ``HOTSWAP`` -- the same role as
        a row's growth annotation, because they are the same measurement -- and
        the labels are dim so the numbers stand out.
        """
        def value_of(key):
            result = self.sys_growth.get(key)
            if not result:
                return '-'
            growth, interval = result
            if self.opts.growth_style == 'rate':
                if interval < self.RATE_MIN_SECS:
                    return '-'
                minutes = interval / 60.0
                per_day = (growth * 1440.0 / minutes) if minutes > 0 else 0
                return f'{human_kb(per_day)}/d'
            return str(human_kb(growth))

        if os.geteuid() == 0:
            keys = ('Used', 'TOTALS', 'ShTmp', 'OthK', 'OthU')
        else:
            keys = ('Used', 'TOTALS', 'ShTmp', 'Oth')
        values = {key: value_of(key) for key in keys}
        width = max(len(text) for text in values.values())
        self.sys_growth_width, self._sysgrow_small = self._debounced_width(
            self.sys_growth_width, width, self._sysgrow_small)
        hot = semantic_attr(Theme.HOTSWAP)
        segments = [('    ', None)]
        for idx, key in enumerate(keys):
            if idx:
                segments.append(('  ', None))
            segments.append((f'Δ{key} ', curses.A_DIM))
            segments.append((f'{values[key]:<{self.sys_growth_width}}', hot))
        # rstrip the trailing pad before appending the shared interval, exactly
        # as the single-string form did, so the gap stays one space.
        text, attr = segments[-1]
        segments[-1] = (text.rstrip(), attr)
        if self.opts.growth_style != 'off':
            result = next((self.sys_growth.get(key) for key in keys
                           if self.sys_growth.get(key)), None)
            if result:
                segments.append((f' [{ago_str(result[1])}]', curses.A_DIM))
        return [(text, attr) for text, attr in segments if text]

    def prc_group(self, group):
        """Process on group"""
        do_smaps = False
        if group.o_rollup_summary:
            do_smaps, _ = self.test_delta(
                    group, group.rollup_summary, group.o_rollup_summary)
        else:
            do_smaps = True
        if self.opts.others:
            do_smaps = False

        for prc in list(group.prcset):
            if self.opts.groupby == 'exe':
                group.summary['info'] = f'{prc.exebasename}'
            elif self.opts.groupby == 'cmd':
                group.summary['info'] = f'{prc.cmdline_trunc}'
            elif self.is_cgroup():
                group.summary['info'] = cgroup_leaf(group.key)
            else:
                group.summary['info'] = f'{prc.pid} {prc.cmdline_trunc}'
            if do_smaps:
                global read_smaps
                read_smaps += 1
                smaps_lines = prc.get_smaps_lines()
                if prc.why_not:
                    group.prcset.remove(prc)
                    continue
                chunks = prc.make_chunks(smaps_lines)
                prc.categorize_chunks(chunks)
                summary = prc.summarize_chunks(chunks)
                self.add_to_summary(summary, group.summary)
        if self.opts.others:
            self.add_to_summary(group.rollup_summary, group.summary)
        group.summary['pss'] = group.rollup_summary['ptotal']
        group.summary['pswap'] = group.rollup_summary['pswap']
        group.summary['cpu%'] = group.rollup_summary['cpu%']

        if not group.prcset:
            group.alive = False
            do_smaps = False

        if self.opts.others:
            return
        if not do_smaps:
            group.summary = group.o_summary
            if group.summary and group.rollup_summary:
                group.summary['cpu%'] = group.rollup_summary['cpu%']
            return

        if self.debug:
            DB(2, f'{group.key} summary: {group.summary}')

        group.is_changed = False
        if group.o_summary:
            group.is_changed, group.delta_pss = self.test_delta(
                    group, group.summary, group.o_summary)
        else:
            group.is_changed = True

        if group.first_summary:
            group.growth_pct = 100*(group.summary['ptotal']
                - group.first_summary['ptotal'])/group.first_summary['ptotal']
        else:
            group.first_summary = group.summary

        if group.is_changed:
            group.o_summary = group.summary
        elif group.o_summary:
            group.summary = group.o_summary

        if self.debug:
            DB(1 if group.is_changed else 5, f'{group.key}:', group.summary)

    def is_cgroup(self):
        """True when the current grouping is a cgroup v2 grouping."""
        return is_cgroup_groupby(getattr(self.opts, 'groupby', None))

    def cgroup_view(self):
        """Active cgroup memory view: ``'footprt'`` or ``'kcharge'``.

        Derived from the grouping method so there is a single source of truth:
        ``-g cgroup`` shows the footprint view, ``-g cgroupCharge`` the kernel
        ``memory.current`` view.
        """
        return ('kcharge' if getattr(self.opts, 'groupby', None) == 'cgroupCharge'
                else 'footprt')

    def total_key(self):
        """Column key holding the current view's memory total."""
        if self.is_cgroup() and self.cgroup_data_seen:
            return self.cgroup_view()
        return 'ptotal'

    def view_columns(self):
        """Ordered column keys rendered for the current mode/view.

        A single place decides column presence and order, so the header, every
        row, and the TOTALS row stay aligned. ``number``/``info`` are emitted
        separately (they are not columns). In cgroup mode ``ptotal`` is kept as
        a left-hand PSS reference (the tool's namesake) and the kernel
        decomposition that reconciles to the charge total follows it.
        """
        cols = []
        if self.opts.cpu:
            cols.append('cpu%')
        if self.is_cgroup() and self.cgroup_data_seen:
            if self.opts.psi:
                cols.append('memPSI%')
            cols.append('ptotal')
            cols.append('')  # spacer: ptotal is a reference, not part of the sum
            cols += ['anon', 'cache', 'kmem']
            if self.cgroup_view() == 'footprt':
                cols.append('swap')
            cols.append('oK')
            cols.append(self.total_key())
        else:
            cols.append('pswap')
            if self.opts.others:
                cols.append(self.OTHER_KEYS[0])
            else:
                cols += list(self.OTHER_KEYS)
            cols += ['data', 'ptotal']
        if self.debug:
            cols.append('pss')
        return cols

    def pr_summary(self, lead, summary, attr=None, to_head=False):
        """Print a summary of memory use (columns from view_columns()).

        The row is emitted as attribute runs (see :meth:`emit_segments`) so the
        growth annotation, the new-group marker and the grouping name can carry
        a colour role.  The runs concatenate to exactly the one-line string this
        method has always produced, so the column layout is unchanged.
        """
        segments = self.summary_segments(lead, summary)
        if attr is not None:
            # A caller-supplied whole-row attribute wins: collapse the runs so
            # its style is not mixed with the role colours.
            self.emit_segments([(''.join(text for text, _ in segments), attr)],
                               to_head=to_head)
        else:
            self.emit_segments(segments, to_head=to_head)

    def summary_segments(self, lead, summary):
        """One summary row as ``(text, attr)`` runs (see :meth:`pr_summary`).

        Zero-valued cells are dimmed: in a typical report most cells of most
        rows are zero, and de-emphasizing them is what makes the non-zero ones
        readable at a glance.  ``n/a`` is dimmed for the same reason.
        """
        segments = []
        combined = bool(self.opts.others)
        for item in self.view_columns():
            if not item:
                segments.append(('  ', None))
                continue
            value = summary.get(item, 0)
            if value is None:
                segments.append((f'{"n/a":>{self.fwidth}}', curses.A_DIM))
                continue
            if item in ('cpu%', 'memPSI%'):
                segments.append((f'{value:>{self.fwidth}.1f}',
                                 None if value else curses.A_DIM))
                continue
            if combined and item == self.OTHER_KEYS[0]:
                value = sum(summary.get(key, 0) for key in self.OTHER_KEYS)
            mbytes = int(round(value*1024/self.divisor))
            if self.divisor > 1:
                text = f'{mbytes:>{self.fwidth},}'
            else:
                text = f'{human(mbytes):>{self.fwidth}}'
            segments.append((text, curses.A_DIM if mbytes == 0 else None))
        num = summary['number']
        segments.append(self.lead_segment(lead))
        segments.append((f'{-num}' if num <= 0 else f'{num}x', None))
        segments.append((f' {summary["info"]}', self.info_attr(lead)))
        return segments

    def lead_attr(self, lead):
        """Colour role for a summary row's inline growth/marker column."""
        marker = lead.strip()
        if marker == 'A':
            return semantic_attr(Theme.SUCCESS)
        if marker == 'T':
            return semantic_attr(Theme.EMPHASIS)
        if marker in ('O', 'x'):
            return curses.A_DIM
        if marker:
            # The only other value this column holds is a growth annotation.
            return semantic_attr(Theme.HOTSWAP)
        return None

    def info_attr(self, lead):
        """Colour role for the grouping name at the end of a summary row.

        Only the TOTALS row is a title.  A group's own name stays default
        styling on purpose: the stock 'default' theme's EMPHASIS is white on the
        terminal's default background, which a light terminal cannot show, and
        this column is the densest text on screen (see the roles comment above).
        """
        marker = lead.strip()
        if marker in ('O', 'x'):
            return curses.A_DIM
        if marker == 'T':
            return semantic_attr(Theme.EMPHASIS)
        return None

    def lead_segment(self, lead):
        """The lead (growth/marker) cell of a summary row, plus its pad.

        The TOTALS row shows the ``growth`` label instead of its ``T`` marker:
        the row's own info already reads TOTALS, so the marker is redundant
        there, and putting the label on the last header line places it directly
        above the annotations it titles.  With growth ``off`` there is no
        annotation column to title, so ``T`` is kept.
        """
        if lead == 'T' and getattr(self.opts, 'growth_style', 'off') != 'off':
            label = self.growth_lead_header()
            return (f'{label} ', self.lead_attr(self.GROWTH_HEADER))
        return (f' {lead:<{self.lead_width}} ', self.lead_attr(lead))

    @staticmethod
    def get_meminfo():
        """Get most vital stats from /proc/meminfo'"""
        meminfofile = '/proc/meminfo'
        meminfoKB = {'MemTotal': 0, 'MemAvailable': 0, 'Dirty': 0,
                     'Shmem': 0, 'SUnreclaim': 0, 'KernelStack': 0,
                     'PageTables': 0}
        keys = list(meminfoKB.keys())

        with open(meminfofile, encoding='utf-8') as fileh:
            for line in fileh:
                match = re.match(r'^([^:]+):\s+(\d+)\s*kB', line)
                if not match:
                    continue
                key, value = match.group(1), int(match.group(2))
                if key not in keys:
                    continue
                meminfoKB[key] = value
                keys.remove(key)
                if not keys:
                    break
        assert not keys, f'ALERT: cannot get vitals ({keys}) from {meminfofile}'
        return meminfoKB

    @staticmethod
    def oth_text(meminfoKB, appKB):
        """The leader line's unaccounted-memory field(s).

        ``Oth`` is whatever ``Tot - Avail - Tmp - ptotal`` leaves out: kernel,
        reserve, drivers, imprecision, and -- when not root -- every process
        whose ``smaps`` cannot be read.  Because ``ptotal`` is only complete
        when every process is accounted for, the split into kernel memory
        (``OthK = SUnreclaim + KernelStack + PageTables``) and the remainder
        (``OthU``) is only shown when running as root; otherwise the residual
        also absorbs other users' processes, so a single ``Oth`` is shown
        rather than mislabelling that memory as userland ``OthU``.
        """
        othk = (meminfoKB['SUnreclaim'] + meminfoKB['KernelStack']
                + meminfoKB['PageTables'])
        if appKB:
            other = (meminfoKB['MemTotal'] - appKB
                     - meminfoKB['MemAvailable'] - meminfoKB['Shmem'])
            if os.geteuid() == 0:
                return (f' OthK={human(othk*1024)}'
                        f' OthU={human((other-othk)*1024)}')
            return f' Oth={human(other*1024)}'
        if othk:
            return f' OthK={human(othk*1024)}'
        return ''

    def get_vmstat(self):
        """Get most vital stats from /proc/vmstat."""
        def make_ns(now):
            ns = SimpleNamespace()
            ns.base_time = now
            ns.base_value = 0
            ns.last_time = now
            ns.last_value = 0
            ns.rate = 0
            return ns

        infofile = '/proc/vmstat'
        now = time.monotonic()
        if not self.vmstat:
            self.vmstat = {'pgmajfault': make_ns(now)}
        info = self.vmstat
        keys = list(info.keys())

        with open(infofile, encoding='utf-8') as fileh:
            for line in fileh:
                match = re.match(r'^([^\s]+)\s+(\d+)$', line)
                if not match:
                    continue
                key, value = match.group(1), int(match.group(2))
                if key not in keys:
                    continue
                ns = info[key]
                ns.base_time, ns.base_value = ns.last_time, ns.last_value
                ns.last_time, ns.last_value = now, value
                ns.rate = 0
                if ns.last_time > ns.base_time:
                    ns.rate = int(round((ns.last_value - ns.base_value)
                            / (ns.last_time - ns.base_time)))
                keys.remove(key)
                if not keys:
                    break
        assert not keys, f'ALERT: cannot get vitals ({keys}) from {infofile}'
        return self.vmstat

    def loop(self, now, is_first, regroup=False):
        """one loop thru all pids"""
        def sort_kernel_prcs():
            self.kernel_prcs = sorted(self.kernel_prcs, reverse=True,
                  key=lambda x: x.cpu.percent if x.cpu else 0)

        def pr_top_of_report(appKB):
            nonlocal self, meminfoKB, wanted_prcs, total_user_pids, kernel_cpu
            nonlocal major_fault_rate
            windowed = bool(self.window)
            # Pin the key legend as the very first header row on every redraw
            # so the available keys are always in evidence.
            self.emit_key_legend()
            # An armed 'z' confirmation sits directly under the legend, so the
            # question is impossible to miss; its countdown ticks down with each
            # refresh of the report.
            if self.zap_confirm_active():
                # Destructive: DANGER (not underline) so it cannot be mistaken
                # for the growth annotation it sits under.
                self.emit(self.zap_confirm_text(), to_head=True,
                          attr=curses.A_BOLD | semantic_attr(Theme.DANGER))
            resume = False
            # print timestamp of report
            leader = '' if windowed else '--- '
            leader += f'{now.strftime("%H:%M:%S")}'
            self.emit(leader, to_head=True, resume=resume,
                      attr=curses.A_BOLD if self.loop_num % 2 else None)
            leader = ''
            resume = True

            used = meminfoKB["MemTotal"] - meminfoKB["MemAvailable"]
            leader += f' Tot={human(meminfoKB["MemTotal"]*1024)}'
            leader += f' Used={human(used*1024)}'
            leader += f' Avail={human(meminfoKB["MemAvailable"]*1024)}'
            leader += self.oth_text(meminfoKB, appKB)
            leader += f' Sh+Tmp={human(meminfoKB["Shmem"]*1024)}'
            if len(wanted_prcs) < total_user_pids:
                leader += f' PIDs={len(wanted_prcs)}/{total_user_pids}'
            else:
                leader += f' PIDs={total_user_pids}'

            self.emit(leader, to_head=True, resume=resume)
            resume = True

            # System growth line (attribution identity), only when enabled and
            # a baseline exists.
            if self.opts.growth_style != 'off' and self.sys_growth:
                self.emit_segments(self.sys_growth_segments(),
                                   to_head=True, resume=False)
                resume = True

            if self.has_zram(): # second line if zRAM
                proj = self.zram_projector
                # The zRAM line reports estimates of effective RAM, so its
                # values carry INFO and its labels are dim; the zRAM size itself
                # stays bold to read as the headline number of the line.
                info = semantic_attr(Theme.INFO)
                segments = []
                if self.opts.cpu:
                    segments.append((f'{kernel_cpu:8.1f}%/ker ', None))
                segments += [
                    ('MajF/s=', curses.A_DIM),
                    (f'{major_fault_rate} ', info),
                    (' zRAM=', curses.A_DIM),
                    (f'{human(self.zram_projector.meminfo.MemZram)}',
                     curses.A_BOLD | info),
                    (' CR=', curses.A_DIM),
                    (f'{proj.ratio:.1f}', info),
                    (' eTot:', curses.A_DIM),
                    (f'{proj.human_pct(proj.e_max_used)}', info),
                    (' eUsed:', curses.A_DIM),
                    (f'{proj.human_pct(proj.e_used)}', info),
                    (' eAvail:', curses.A_DIM),
                    (f'{proj.human_pct(proj.e_avail)}', info),
                ]
                # leader += f' Dirty={human(meminfoKB["Dirty"]*1024)}'
                self.emit_segments(segments, to_head=True, resume=False)
            elif self.opts.cpu: # second line if reporting cpu
                resume = False
                leader = f'{kernel_cpu:8.1f}%/ker'
                sort_kernel_prcs()
                for prc in self.kernel_prcs[0:2]:
                    nickname = prc.cpu.get_nickname()
                    leader += f'    {prc.cpu.percent:.2f}% {nickname}'
                self.emit(leader, to_head=True, resume=resume)

            if self.opts.psi: # PSI leader block (opt-in, one line per resource)
                for idx, psi_line in enumerate(
                        format_pressure_lines(self.pressure)):
                    # The heading row is a title (EMPHASIS); the value rows keep
                    # their default styling until a threshold policy is agreed.
                    attr = (curses.A_BOLD | semantic_attr(Theme.EMPHASIS)
                            if idx == 0 else None)
                    self.emit(psi_line, to_head=True, resume=False, attr=attr)

        self.loop_num += 1
        meminfoKB = self.get_meminfo()
        vmstat = self.get_vmstat()
        major_fault_rate = vmstat['pgmajfault'].rate
        self.zram_projector.compute_effective(meminfoKB)
        self.pressure = read_system_pressure() if self.opts.psi else {}
        total_user_pids = 0
        total_kernel_pids = 0
        kernel_cpu = 0
        all_pids = []
        wanted_prcs = {}
        self.kernel_prcs = []

        self.prep_new_loop(regroup)

        if self.window and (is_first or regroup):
            pr_top_of_report(appKB=0)
            self.emit('   WORKING .... be patient ;-)', attr=curses.A_REVERSE)
            self.emit('   HINTS:')
            self.emit('     - Type "?" to open Help Screen')
            self.emit('     - Type "Ctrl-C" to exit program')
            if os.geteuid() != 0:
                self.emit('     - Run "sudo pmemstat" to show all PIDs!',
                          attr=curses.A_BOLD)

            self.window.render()
            self.window.clear()

        with os.scandir('/proc') as it:
            for entry in it:
                # if re.match(r'^\d+$', entry.name):
                if entry.name.isdigit():
                    all_pids.append(entry.name)

        prcs = []
        for pid in all_pids:
            ## print(f'DBDB pid={pid} self.opts.pids={opts.pids}')
            prc = self.prcs.get(pid, None)
            if not prc:
                prc = ProcMem(int(pid))
                self.prcs[pid] = prc
            else:
                prc.is_new = False
            prcs.append(prc)

        # do cpu together that stats are consistent
        if self.opts.cpu:
            SysStat.refresh()
            percent = 0
            for prc in prcs:
                if prc.wanted or prc.kernel:
                    percent = prc.refresh_cpu()
                if prc.kernel:
                    kernel_cpu += percent
                    self.kernel_prcs.append(prc)

        for prc in prcs:
            prc.prc_pid()
            pid = prc.pid
            ## if str(pid) in opts.pids:
                ## print(f'DBDB pid={pid} dir={vars(prc)}')

            # Count only *after* classification: prc_pid() -> get_cmdline() is
            # what sets prc.kernel for kernel threads (empty cmdline).  Counting
            # here, rather than in the CPU block above, keeps the denominator
            # correct on the very first loop -- fresh ProcMem objects default to
            # kernel=False "until proven otherwise" -- and makes it independent
            # of whether CPU reporting is on (it is forced off by -W, which used
            # to leave the count at 0).  See the PIDs=shown/total leader field.
            if prc.kernel:
                total_kernel_pids += 1
            else:
                total_user_pids += 1

            if prc.wanted:
                wanted_prcs[pid] = prc
                if self.debug:
                    DB(1, f'Doing pid={pid} exe={prc.exebasename} cmd={prc.cmdline_trunc}')
            else:
                if self.debug:
                    DB(4, f'Unwanted pid={pid} exe={prc.exebasename}')

        # all pids have been processed into groups.
        # for each group, if it has changed, sum all the smaps for the group
        # if the group rollup_summary indicates enough change
        grand_summary = ProcMem.make_summary_dict(info=f'--TOTALS in {self.units} --')
        self.cgroup_data_seen = False
        self.cgroup_raw = {}
        self.cgroup_charge_keys = set()
        self.cgroup_keys = {g.key for g in self.groups.values()
                            if isinstance(g.key, str) and g.prcset}
        for group in self.groups.values():
            if group.alive:
                self.prc_group(group)
                if self.is_cgroup():
                    self.update_cgroup_summary(group)
        # cgroup v2 accounting is hierarchical, so reduce every row to its own
        # ("local") share before totalling - that keeps the rows adding up to
        # TOTALS. A sum of pressure percentages is meaningless, so memPSI% is
        # shown as n/a there.
        if self.is_cgroup():
            self.apply_cgroup_locals()
            grand_summary['memPSI%'] = None
        for group in self.groups.values():
            if group.alive:
                self.add_to_summary(group.summary, grand_summary)

        # detect changed group on basis of differing PIDs contributing

        if grand_summary['number'] == 0:
            print('DONE: no pids to report ... exiting now')
            sys.exit(0)

        # Growth: feed the trackers (always, even when the annotation is off)
        # and pick which rows may annotate.
        top = self.opts.growth_top
        self.opts.growth_top_n = None if top == 'all' else int(top)
        if regroup or self.opts.growth_style != self._last_growth_mode:
            self.lead_width = 1
            self.sys_growth_width = 1
            self._lead_small = 0
            self._sysgrow_small = 0
            self._last_growth_mode = self.opts.growth_style
        self.update_growth(time.monotonic(), grand_summary, meminfoKB)
        self.history_update()
        alive_for_growth = {k: g for k, g in self.groups.items() if g.alive}
        allowed = self.select_growth_rows(alive_for_growth)
        # Reserve room for the 'growth' header whenever the annotation is on,
        # even if every row is currently blank (no baseline yet / nothing over
        # the gate), so the header can never overlap 'key/info'.
        need = self.growth_lead_min_width()
        for key, group in alive_for_growth.items():
            group.growth_txt = (self.growth_text(group)
                                if key in allowed else '')
            need = max(need, len(group.growth_txt))
        self.lead_width, self._lead_small = self._debounced_width(
            self.lead_width, need, self._lead_small)

        # print header and  grand totals
        pr_top_of_report(appKB=grand_summary['ptotal'])

        header = ''
        for item in self.view_columns():
            if not item:
                header += '  '
                continue
            label = ('other' if (self.opts.others and item == self.OTHER_KEYS[0])
                     else item)
            header += f'{label:>{self.fwidth}}'
        # The growth column sits between the numeric columns and 'key/info'.
        # Titles are EMPHASIS.  The lead column is blank on this line: its
        # 'growth' label rides on the TOTALS row now (see lead_segment),
        # directly above the annotations it titles, while this line reserves
        # the same width so key/info does not move.
        # The '(grouping by sort)' descriptor names the current view -- what a
        # reader looks for first when the numbers surprise them -- so it is
        # SUCCESS rather than dim meta text.
        emphasis = curses.A_BOLD | semantic_attr(Theme.EMPHASIS)
        self.emit_segments([
            (header, emphasis),
            (self.growth_lead_blank(), None),
            (' key/info', emphasis),
            (f' ({self.opts.groupby} by {self.get_sortby()})',
             curses.A_BOLD | semantic_attr(Theme.SUCCESS)),
        ], to_head=True)
        self.pr_summary('T', grand_summary, to_head=True)

        alive_groups = {}
        for key, group in self.groups.items():
            if group.alive:
                alive_groups[key] = group
                if not group.summary:
                    DB(0, 'no summary:', str(group))

        if self.get_sortby() == 'cpu':
            sorted_keys = sorted(alive_groups.keys(), key=lambda x:
                (-round(alive_groups[x].summary['cpu%'], 1),
                    str(alive_groups[x].key).lower()))
        elif self.get_sortby() == 'name':
            sorted_keys = sorted(alive_groups.keys(),
                key=lambda x: str(alive_groups[x].key).lower())
        elif self.get_sortby() == 'growth':
            def rank(group):
                if group.growth_val is None:
                    return None
                if self.opts.growth_style in ('both', 'rate'):
                    return group.growth_rate
                return group.growth_val
            sorted_keys = sorted(alive_groups.keys(),
                key=lambda x: (rank(alive_groups[x]) is not None,
                               rank(alive_groups[x]) or 0.0), reverse=True)
        else:
            sorted_keys = sorted(alive_groups.keys(),
                key=lambda x: (alive_groups[x].summary.get(self.total_key()) or 0),
                reverse=True)

        limit = self.window.scroll_view_size if self.is_fit_opted() else 1000000
        total_limit = ((grand_summary.get(self.total_key()) or 0)
                       * self.opts.top_pct / 100) * 1.001
        others_summary = None
        running_summary = ProcMem.make_summary_dict(info='---- RUNNING ----')
        shown_cnt = 0
        self.groups_by_line = {}
        for key in sorted_keys:
            group = alive_groups[key]
            self.add_to_summary(group.summary, running_summary)
            haystack = group.summary['info']
            if self.is_cgroup():
                haystack += ' ' + str(group.key)
            if (self.opts.search in haystack and
              shown_cnt < limit-1
              and (running_summary.get(self.total_key()) or 0) <= total_limit):
                if group.alive and (group.is_new or group.is_changed or self.window):
                    attr = None
                    if self.window:
                        current_row = self.window.body.row_cnt
                        self.groups_by_line[current_row] = group
                    lead = 'A' if group.is_new else group.growth_txt
                    self.pr_summary(lead, group.summary, attr=attr)
                    # Show confirmation prompt right after the selected line
                    if (self.window and self.confirmation.active and
                        current_row == self.window.pick_pos):
                        prompt = f'  Type "y" to kill: {self.confirmation.identity} (ESC to cancel)'
                        if self.confirmation.input_buffer:
                            prompt += f' [{self.confirmation.input_buffer}_]'
                        # DANGER rather than reverse-video: the row under the
                        # pick is already reversed, and a kill prompt should not
                        # look like another picked row.
                        self.emit(prompt,
                                  attr=curses.A_BOLD | semantic_attr(Theme.DANGER))
                    shown_cnt += 1
                    # DB(0, f'obj: {vars(obj)}')
            elif is_first or self.opts.window:
                if not others_summary:
                    others_summary = ProcMem.make_summary_dict(info='---- OTHERS ----')
                    if self.is_cgroup():
                        others_summary['memPSI%'] = None
                self.add_to_summary(group.summary, others_summary)
        if others_summary:
            self.pr_summary('O',  others_summary)

        remainder = limit - self.window.body.row_cnt if self.is_fit_opted() else 1000000
        for group in self.groups.values():
            if not group.alive and group.o_summary and remainder > 0:
                remainder -= 1
                self.pr_summary('x', group.o_summary)
        if not self.window:
            self.emit('')

    def emit_key_legend(self):
        """Emit the key legend (first header line) with the live search field.

        The legend is left-aligned so '[?]:help' sits above the first digit of
        the leader's 'Tot=' value; the search field is appended after it and is
        drawn non-dimmed to signal that it is live. When not running as root the
        leftmost pad is filled with a '--user' marker (the flag that selects the
        degraded, own-processes-only view) so it is always obvious the report is
        partial. No-op outside window mode, where there are no interactive keys.

        The line goes through ``add_fancy_header(mode='Underline')``: the keys
        are underlined and their '[x]' brackets are dropped, which makes the
        legend noticeably more compact than the old literal-bracket rendering,
        and the trailing '/pattern' is styled as live search text.  (The mode
        name is matched case-sensitively by console-window: 'Underline' -- a
        lowercase spelling silently falls back to reverse-video keys.)  The
        whole line is additionally dimmed via ``line_attr``: this is passive
        help, so it should not compete with the value lines for attention.
        """
        if not self.window:
            return
        # 'HH:MM:SS Tot=' is the leader prefix; the legend sits under the first
        # digit of the Tot= value, shifted 5 columns left so the optional
        # '[z]ap' entry does not push the trailing live search field any
        # further right.
        pad = max(0, len('00:00:00 Tot=') - 5)
        legend = self.KEY_LEGEND
        if self.zap_key_offered():
            legend += self.KEY_LEGEND_ZAP
        if os.geteuid() == 0:
            lead = ' ' * pad
        else:
            # Degraded (non-root) run: stamp the header's leftmost pad with the
            # '--user' flag. It is padded to the pad width so it fills the blank
            # leading space without shifting the legend itself.
            lead = self.KEY_LEGEND_USER.ljust(pad)
        if self.search_bar.is_active:
            before = self.search_bar.text[:self.search_bar.cursor_pos]
            after = self.search_bar.text[self.search_bar.cursor_pos:]
            search, search_attr = f'/{before}|{after}', curses.A_REVERSE
        else:
            search, search_attr = f'/{self.opts.search or ""}', curses.A_BOLD
        self.window.add_fancy_header(f'{lead}{legend} {search}',
                                     mode='Underline',
                                     search_pattern_attr=search_attr,
                                     line_attr=curses.A_DIM)
        self.window.calc()

    def zap_key_offered(self):
        """Whether '[z]ap' belongs in the key legend.

        Root only (the cross-run ledger's coverage depends on root's complete
        process view) and only while the growth annotation is showing: with
        growth 'off' there are no growth numbers on screen, so offering a reset
        of the growth baseline would advertise an action with no visible
        effect.
        """
        return (os.geteuid() == 0
                and getattr(self.opts, 'growth_style', 'off') != 'off')

    def emit_segments(self, segments, to_head=False, resume=False):
        """Emit one row built from ``(text, attr)`` runs.

        console-window stores a row as one attribute run per ``add_*`` call and
        *appends* a run when ``resume=True`` (see ``ConsoleWindow._add``), so a
        coloured row is several calls that all land on the same row.  The runs
        must concatenate to exactly the plain row text: the pad truncates each
        call to the *remaining* width, which is what keeps the columns aligned.
        """
        segments = [(text, attr) for text, attr in segments if text]
        if not segments:
            return
        if not self.window:
            # The one-shot report has no attributes at all: keep its output
            # byte-identical by printing the joined row once.
            self.emit(''.join(text for text, _ in segments),
                      to_head=to_head, resume=resume)
            return
        for idx, (text, attr) in enumerate(segments):
            self.emit(text, to_head=to_head, attr=attr,
                      resume=bool(resume) or idx > 0)

    def emit(self, line, to_head=False, attr=None, resume=False):
        """ Emit a line of the report"""
        if self.window:
            if to_head:
                self.window.add_header(line, attr=attr, resume=resume)
                self.window.calc()
            else:
                self.window.add_body(line, attr=attr, resume=resume)
        else:
            print(('' if resume else '\n') + line, end='')

    # One early return per modal owner (confirmation, zap, search, '/', 'z',
    # ENTER, spinner) is clearer than a nested dispatch tower here.
    # pylint: disable=too-many-return-statements
    def do_key(self, key):
        """Route one key: modal widgets first, then the spinner's router.

        Modal widgets (kill confirmation, the zap prompt, the search bar) own
        every key while live, so no action can fire underneath a pending
        destructive question.  Any key the spinner consumes requests a refresh,
        because every option changes what the report draws.
        """
        if key is None:
            return
        if self.confirmation.active:
            self._handle_kill_key(key)
            return
        if self.zap_confirm_active():
            self.handle_zap_confirm_key(key)
            self.needs_refresh = True
            return
        if self.search_bar.is_active and self.search_bar.handle_key(key):
            self.needs_refresh = True
            return
        if key == ord('/'):
            # Inline search: the bar owns keys until ENTER accepts or ESC clears.
            self.search_bar.start(self.opts.search)
            self.window.passthrough_mode = True
            return
        if key == ord('z'):
            # Runtime equivalent of --reset-history-now (root only; the key is
            # only passed here when root -- see window_loop).  It is
            # destructive, so this only *arms* the confirmation prompt; 'y'
            # commits it and ESC / expiry does not.  Kill mode owns its own
            # confirmation, so it is left alone to avoid two live prompts.
            if not self.opts.kill_mode:
                self.zap_confirm_start()
            self.needs_refresh = True
            return
        if key in (curses.KEY_ENTER, 10):
            # ENTER never closes a screen (ESC is the one way back); in kill mode
            # it selects the picked row.
            if self.opts.kill_mode and self.stack.is_curr(MAIN_ST):
                self._start_kill_confirmation()
            return
        if key in self.spin.keys:
            self.spin.do_key(key, self.window)
            if key == ord('u'):
                self._set_units()
            elif key == ord('K') and self.stack.is_curr(MAIN_ST):
                self.window.set_pick_mode(self.opts.kill_mode)
            self.needs_refresh = True

    def _start_kill_confirmation(self):
        """Ask 'y' to kill every process in the picked row."""
        win = self.window
        group = self.groups_by_line.get(win.pick_pos, None)
        if not group:
            # Nothing picked: ENTER just leaves kill mode.
            self.opts.kill_mode = False
            win.set_pick_mode(False)
            return
        pids = [x.pid for x in group.prcset]
        pid_count = len(pids)
        pid_label = f'[{pid_count} {"PID" if pid_count == 1 else "PIDs"}]'
        self.confirmation.start(action_type='kill',
                                identity=f'{group.summary["info"]} {pid_label}',
                                mode='y')
        win.passthrough_mode = True
        self.needs_refresh = True

    def _handle_kill_key(self, key):
        """Feed one key to the live kill confirmation and act on the answer."""
        result = self.confirmation.handle_key(key)
        if result == 'confirmed':
            group = self.groups_by_line.get(self.window.pick_pos, None)
            if group:
                pids = [x.pid for x in group.prcset]
                ok, message = KillThem(pids).do_kill()
                self.window.flash(f'{"✓" if ok else "✗"} {message}', duration=1.0)
            self.confirmation.cancel()
            self.window.passthrough_mode = False
            self.opts.kill_mode = False
            self.window.set_pick_mode(False)
        elif result == 'cancelled':
            self.confirmation.cancel()
            self.window.passthrough_mode = False
        self.needs_refresh = True

    def theme_label(self):
        """The appearance the picker should show as current."""
        if not colors_enabled():
            return MONO_THEME
        return getattr(self.opts, 'theme', '') or 'default'

    def theme_choices(self):
        """Every appearance the picker cycles, in order."""
        return list(Theme.list_all()) + [MONO_THEME]

    def apply_theme(self, name):
        """Make *name* the live appearance ('mono' turns colour off) and persist it.

        ``console-window`` installs colour pairs per terminal and the picker's
        choice must outlive the run, so this is the one place that touches both
        the pair table and the preference file.
        """
        mono = name == MONO_THEME
        self.opts.colors = not mono
        self.opts.theme = '' if mono else name
        set_colors_enabled(not mono)
        if self.window:
            self.window.set_theme('default' if mono else name)
        prefs = self.prefs
        if prefs is not None:
            prefs.set('colors', not mono)
            prefs.set('theme', self.opts.theme)
            prefs.save()
        self.needs_refresh = True
        if self.window:
            self.window.status(f'theme: {self.theme_label()}')

    def cycle_theme(self):
        """Advance the picker by one appearance."""
        choices = self.theme_choices()
        current = self.theme_label()
        idx = choices.index(current) if current in choices else -1
        self.apply_theme(choices[(idx + 1) % len(choices)])

    def window_loop(self):
        """Run the report under console-window's Screen model.

        One window, one ScreenStack: MAIN draws the report, HELP the key list,
        THEMES the picker.  Keys flow through the spinner (which owns the
        per-screen scopes) and the stack calls the matching ``<action>_ACTION``
        method, so there is no central key tower here.

        The report's window model survives between frames -- console-window
        repaints from it -- so only a *refresh* costs a /proc scan: idle timeout,
        a key that changes what is drawn, or returning from another screen.
        """
        # The window is created without app keys because the spinner's key set
        # is only known once it is registered; set_handled_keys() installs it.
        #
        # return_if_pos_change=False is essential for smooth navigation: with
        # the default (True), prompt() returns None *without* rendering as soon
        # as a j/k/arrow key moves the viewport, and "None" is this app's
        # timed-refresh signal, so a burst of buffered j/k would queue a full
        # /proc scan per key.  With False, prompt() repaints the existing model
        # itself and returns only on a handled key or the loop_secs timeout, so
        # None really does mean "time to refresh the data".
        window_opts = ConsoleWindowOpts(head_line=True,
                                        return_if_pos_change=False)
        self.window = ConsoleWindow(opts=window_opts)
        self.screens = {MAIN_ST: MainScreen(self),
                        HELP_ST: HelpScreen(self),
                        THEMES_ST: ThemeScreen(self)}
        self.stack = ScreenStack(self.window, self.opts, SCREEN_NAMES,
                                 self.screens)
        # Apply the remembered appearance before the first paint, so the very
        # first frame is already in the user's colours (or in mono).
        self.window.set_theme('default' if not colors_enabled()
                              else (getattr(self.opts, 'theme', '') or 'default'))

        self.spin = OptionSpinner(stack=self.stack)
        spin = self.spin
        # '?' and 'T' are actions: the stack routes each to <name>_ACTION on the
        # current screen.  ENTER is deliberately not a screen key (ESC is the
        # one way back, as in the other tools); it stays kill mode's selector.
        spin.add_key('help', '? - help screen', genre='action')
        spin.add_key('screen_escape', 'ESC - back one screen', keys=27,
                     genre='action')
        spin.add_key('theme_screen', 't - theme picker', genre='action',
                     scope=MAIN_ST)
        spin.add_key('spin_theme', 't - cycle theme', genre='action',
                     scope=THEMES_ST)
        spin.add_key('kill_mode', 'K - kill mode', vals=[False, True],
                     obj=self.opts,
                     comments='Select line + ENTER to kill selected')
        # 'K' again -- but only on the Help screen, where kill mode has no
        # meaning.  A scoped registration subtracts HELP from every earlier
        # binding of the same key, so the two never collide, and the option
        # shows up in the help list while staying off the report's legend.
        spin.add_key('show_keys', 'K - show keys (demo mode)', genre='action',
                     scope=HELP_ST)
        spin.add_key('fit_to_window', 'f - fit rows to window',
                     vals=[False, True], obj=self.opts)
        spin.add_key('groupby', 'g - group by',
                     vals=['exe', 'cmd', 'pid', 'cgroup', 'cgroupCharge'],
                     obj=self.opts)
        spin.add_key('others', 'o - less category detail', vals=[False, True],
                     obj=self.opts)
        spin.add_key('sortby', 's - sort by',
                     vals=['mem', 'cpu', 'name', 'growth'], obj=self.opts)
        # NB: 'L' is a console_window navigation key (end-of-page), so use 'G'.
        spin.add_key('growth_style', 'G - leak/growth mode',
                     vals=['off', 'both', 'growth', 'rate'], obj=self.opts)
        # 't' now opens the theme picker, so the grower cap moves to 'p' (the
        # biggest "pigs").  PSI takes 'P', the letter of its CLI flag (-P).
        spin.add_key('growth_top', 'p - top-N growers', vals=['all', '10'],
                     obj=self.opts)
        spin.add_key('units', 'u - memory units', vals=['MB', 'KB', 'human'],
                     obj=self.opts)
        spin.add_key('cpu', 'c - show cpu', vals=[False, True], obj=self.opts)
        spin.add_key('psi', 'P - show PSI (header + memPSI%)',
                     vals=[False, True], obj=self.opts)
        handled = set(spin.keys) | {ord('/'), curses.KEY_ENTER, 10}
        if os.geteuid() == 0:
            # 'z' zaps the growth history; offered only to root, since the
            # cross-run ledger's coverage depends on root's process view.
            handled.add(ord('z'))
        self.window.set_handled_keys(handled)
        was_groupby, was_others = self.opts.groupby, self.opts.others
        while True:
            if (self.opts.groupby != was_groupby
                    or self.opts.others != was_others):
                # A grouping change rebuilds every row, so it needs a new sample
                # rather than a repaint of the old one.
                was_groupby, was_others = self.opts.groupby, self.opts.others
                self.regroup_pending = True
                self.needs_refresh = True
            current = self.stack.curr
            screen = self.screens[current.num]
            if current.num != MAIN_ST:
                # Auxiliary screens are rebuilt from scratch every frame; the
                # report's model is kept so keeping it costs no /proc scan.
                self.window.clear()
            screen.draw_screen()
            self.window.render()
            key = self.window.prompt(self.prompt_secs(self.opts.loop_secs))
            if key is None:
                # Idle timeout: take a fresh sample (and settle an armed 'z'.
                # prompt_secs() shortens the wait while that prompt is armed, so
                # the decision is never late by one loop_secs).
                self.zap_confirm_expire()
                self.needs_refresh = True
            else:
                self.do_key(key)
            self.stack.perform_actions(self.spin)

####################################################################################
###### Screens (console-window ScreenStack model)
####################################################################################
class PmemScreen(Screen):
    """Base screen; the report owner (``PmemStat``) is ``self.app``."""

    app: 'PmemStat'

    def screen_escape_ACTION(self):
        """ESC goes back one screen (MAIN's stack is empty, so it is a no-op)."""
        self.app.stack.pop()


class MainScreen(PmemScreen):
    """The memory report itself.

    The report's window model is kept between frames (console-window repaints
    from it), so this screen re-collects and re-emits only when the app asks for
    a refresh: an idle timeout, a presentation key, or a return from another
    screen.  That is what keeps j/k navigation from rescanning every PID.
    """

    def draw_screen(self):
        app = self.app
        if not app.needs_refresh:
            return
        app.window.clear()
        app.loop(datetime.now(), is_first=app.first_loop,
                 regroup=app.regroup_pending)
        app.first_loop = False
        app.regroup_pending = False
        app.needs_refresh = False

    def on_resume(self):
        # Whatever happened on the other screen (a theme change, a grouping
        # change made before it) the report should be redrawn from new data.
        self.app.needs_refresh = True
        return True

    def help_ACTION(self):
        """'?' opens the Help screen from the report."""
        self.app.stack.push(HELP_ST, self.app.window.pick_pos)

    def theme_screen_ACTION(self):
        """'t' opens the theme picker from the report."""
        self.app.stack.push(THEMES_ST, self.app.window.pick_pos)


class HelpScreen(PmemScreen):
    """The key list plus this app's own notes (zap prompt, sudo hint)."""

    def draw_screen(self):
        app = self.app
        win = app.window
        win.set_pick_mode(False)
        win.add_header("-- HELP SCREEN ['?' or ESC closes Help; Ctrl-C exits ] --",
                       attr=curses.A_BOLD)
        if app.spin:
            app.spin.show_help_nav_keys(win)
            app.spin.show_help_body(win)
        if app.zap_confirm_active():
            # An armed 'z' also asks for its answer here, so the prompt is never
            # invisible while it counts down.
            win.add_body(app.zap_confirm_text(),
                         attr=curses.A_BOLD | semantic_attr(Theme.DANGER))
        if os.geteuid() != 0:
            win.add_body('Hint: run "sudo pmemstat" to show all PIDs',
                         attr=curses.A_BOLD)
        else:
            win.add_body('z - zap the growth history (current values become '
                         'baseline) - asks for "y"/ESC')

    def help_ACTION(self):
        """'?' closes Help again (ESC does the same via screen_escape)."""
        self.app.stack.pop()

    def show_keys_ACTION(self):
        """'K' toggles demo mode: the last key pressed is shown on screen.

        Scoped to this screen (see the registration in ``window_loop``): on the
        report 'K' enters kill mode, and a presentation aid must not compete
        with that.
        """
        win = self.app.window
        win.set_demo_mode(enabled=None)          # None toggles
        state = 'on' if getattr(win.opts, 'demo_mode', False) else 'off'
        win.status(f'demo mode: {state}')
        self.app.needs_refresh = True


class ThemeScreen(PmemScreen):
    """Theme picker over console-window's themes plus pmemstat's 'mono'.

    console-window has no true no-colour theme (its 'dark-mono'/'light-mono' are
    still colour themes), so 'mono' is offered here as a pmemstat appearance:
    the semantic roles collapse to attributes (see ``MONO_ROLES``).
    """

    ROLES = (
        (Theme.HOTSWAP, 'HOTSWAP', 'growth, system deltas, growth label'),
        (Theme.EMPHASIS, 'EMPHASIS', 'titles: column header, TOTALS row'),
        (Theme.INFO, 'INFO', 'zRAM estimates'),
        (Theme.SUCCESS, 'SUCCESS', 'new grouping, view descriptor'),
        (Theme.DANGER, 'DANGER', 'kill / reset confirmations'),
        (Theme.WARNING, 'WARNING', 'reserved for threshold warnings'),
    )

    def draw_screen(self):
        app = self.app
        win = app.window
        win.set_pick_mode(False)
        win.add_header(f'COLOR THEME:  {app.theme_label():^18}', attr=curses.A_BOLD)
        win.add_header('   Press [t] to cycle themes, ESC to return',
                       resume=True)
        swatch = win.glyph('swatch') * 8
        for pair_id, name, description in self.ROLES:
            win.add_body(f'{name:10} {swatch}  {description}',
                         attr=semantic_attr(pair_id))
        win.add_body('')
        win.add_body(f'{MONO_THEME:10} no colour at all (bold/reverse only)',
                     attr=curses.A_DIM)
        win.add_body('The choice is remembered for the next run.', attr=curses.A_DIM)

    def spin_theme_ACTION(self):
        """'t' here advances to the next appearance (and persists it)."""
        self.app.cycle_theme()


# Root auto-elevation is opt-in so the tool never escalates behind the
# user's back; the caller opts in per invocation with ``--sudo`` (which
# ``PMEMSTAT_ARGS`` can supply persistently), never automatically.

# Extra default CLI arguments may be supplied once via PMEMSTAT_ARGS, e.g.:
#     export PMEMSTAT_ARGS='--sudo --psi --loop 3 -s name'
# The value is tokenized like a POSIX shell command line and prepended to the
# real argv, so explicit command-line arguments still take precedence (and any
# positional pids given on the command line are added to those from the env).
ARGS_ENV = 'PMEMSTAT_ARGS'

def args_from_env():
    """CLI arguments supplied via ``PMEMSTAT_ARGS`` (``[]`` when unset).

    ``shlex.split`` performs POSIX-shell tokenization, so quoting works as it
    would on a command line (``--search "two words"`` -> ``['--search',
    'two words']``). Raises ``ValueError`` on unbalanced quotes.
    """
    return shlex.split(os.environ.get(ARGS_ENV, ''))


def resolve_ui_prefs(opts, prefs, environ=None):
    """Starting ``(theme, colors)`` from the CLI, the environment and the prefs.

    Precedence, highest first: ``--theme`` / ``--no-color``, the ``NO_COLOR``
    convention (any non-empty value disables colour), then the saved
    preference.  ``theme`` is ``''`` for console-window's default; the special
    value ``'mono'`` is pmemstat's no-colour pseudo-theme, which also forces
    ``colors`` off and is reported as ``theme=''`` (there is no library theme to
    install -- mono is an attribute mapping).
    """
    env = os.environ if environ is None else environ
    theme = getattr(opts, 'theme', None)
    if not theme:
        theme = prefs.get('theme') or ''
    colors = bool(prefs.get('colors'))
    if env.get('NO_COLOR') or getattr(opts, 'no_color', False):
        colors = False
    if theme == MONO_THEME:
        colors = False
        theme = ''
    return theme, colors


def _resolve_argv(raw_argv, env_text):
    """Effective argv for ``main``; returns ``(argv, error)``.

    ``--save-history-now`` and ``--reset-history-now`` are special: they must
    work from cron regardless of the ambient configuration, so the presence of
    either bypasses ``PMEMSTAT_ARGS`` (and, by extension, every other
    environment setting) entirely.  Otherwise ``env_text`` is tokenized and
    prepended to ``raw_argv``; a tokenization error is returned as a message
    rather than raised.
    """
    raw_argv = list(raw_argv)
    for flag in ('--save-history-now', '--reset-history-now'):
        if flag in raw_argv:
            return [flag], None
    try:
        return shlex.split(env_text) + raw_argv, None
    except ValueError as exc:
        return None, str(exc)

def _isolated_can_import(module_root):
    """Whether an isolated interpreter (``python -I``) would find module_root.

    This mirrors the environment used for the sudo'd process, so we can
    decline cleanly instead of prompting for a password and then failing
    with a confusing ImportError (e.g. for a ``pip install --user`` against
    a system interpreter).
    """
    code = ('import importlib.util, sys; '
            f'sys.exit(0 if importlib.util.find_spec({module_root!r}) else 3)')
    try:
        proc = subprocess.run([sys.executable, '-I', '-c', code],
                              stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, check=False)
    except OSError:
        return False
    return proc.returncode == 0

def _sudo_noninteractive_ok():
    """True if sudo can run without prompting (already authorized/NOPASSWD)."""
    try:
        proc = subprocess.run(['sudo', '-n', 'true'],
                              stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, check=False)
    except OSError:
        return False
    return proc.returncode == 0

def rerun_module_as_root(module_name, args=None):
    """Re-exec ``module_name`` as root via sudo, but only in a safe, opt-in way.

    ``args`` is the effective argv (``PMEMSTAT_ARGS`` already merged in) to
    replay for the root process; it defaults to ``sys.argv[1:]``. Passing the
    merged list matters because ``sudo`` resets the environment, so an
    ``PMEMSTAT_ARGS``-provided flag would otherwise be lost after elevation.

    On success the process is replaced and this function does not return.  It
    returns ``None`` when elevation is not needed (already root), or a short
    failure-reason string -- ``'not-importable'``, ``'no-tty'`` or
    ``'exec-failed'`` -- when elevation did not happen, so callers can choose
    to fail loudly (see :func:`_elevate_or_exit`) instead of silently running
    with partial data.

    Safety properties compared with the previous unconditional re-exec:
      * never runs by default: the caller opts in via ``--sudo`` (which
        can be supplied persistently through ``PMEMSTAT_ARGS``);
      * requires an interactive terminal unless sudo is already authorized
        non-interactively, so scripts/CI cannot hang on a password prompt;
      * re-execs the *same interpreter* in isolated mode (``-I``), so the
        caller's ``PYTHONPATH``, the current directory and the per-user site
        directory are not consulted by the root process. This removes the
        ``sitecustomize.py`` / module-shadowing privilege escalation that the
        previous ``sudo env PYTHONPATH=...`` permitted;
      * declines when the isolated interpreter cannot import the module,
        instead of prompting for a password and then failing.
    """
    if os.geteuid() == 0:
        return None
    module_root = module_name.split('.')[0]
    if not _isolated_can_import(module_root):
        print(f'pmemstat: not elevating to root: "{module_root}" is not '
              'importable by an isolated interpreter (install system-wide, '
              'with pipx, or inside a virtualenv to enable sudo elevation).',
              file=sys.stderr)
        return 'not-importable'
    if not (sys.stdin.isatty() or _sudo_noninteractive_ok()):
        print('pmemstat: not elevating to root (no terminal for a sudo '
              'prompt); run "sudo pmemstat" for full detail.',
              file=sys.stderr)
        return 'no-tty'
    if args is None:
        args = sys.argv[1:]
    cmd = ['sudo', sys.executable, '-I', '-m', module_name] + list(args)
    try:
        os.execvp('sudo', cmd)
    except OSError as exc:
        print(f'pmemstat: could not exec sudo: {exc}', file=sys.stderr)
        return 'exec-failed'
    return 'exec-failed'  # unreachable: a successful execvp never returns

def _handle_sigterm(signum, _frame):
    """Turn SIGTERM into a graceful unwind so the history hook can run."""
    raise SystemExit(128 + signum)


def _elevate_or_exit(opts, effective_argv):
    """Exit fatally when ``--sudo`` was requested but elevation cannot happen.

    A non-importable install (or a missing terminal) used to downgrade silently
    to a user-only run whose warning scrolled off-screen, hiding that the data
    was partial.  When root data was explicitly requested, failing visibly is
    the safer behavior.  Callers invoke this only when not already root.
    """
    reason = rerun_module_as_root('pmemstat.main', effective_argv)
    if reason is None:
        return  # already root; nothing to do
    print(f'pmemstat: FATAL: root elevation was requested (--sudo) but failed '
          f'({reason}); run as root (e.g. "sudo pmemstat") or fix the install.',
          file=sys.stderr)
    sys.exit(1)


def save_history_now(opts):
    """Collect the current stats once and write the ledger, then exit (no UI).

    Intended for cron / system startup: it implies ``--sudo`` (data for all
    processes needs root) and ignores every other option and environment
    setting.  Prints nothing on success and returns ``0``; on failure it prints
    an explanation to stderr and returns ``1``.
    """
    if os.geteuid() == 0:
        return _collect_and_save_history(opts)
    reason = rerun_module_as_root('pmemstat.main', ['--save-history-now'])
    # Reaching here means elevation did not happen (success replaces the
    # process), so any return value is a failure reason.
    print('pmemstat: history update FAILED: could not elevate to root '
          f'({reason})', file=sys.stderr)
    return 1


def reset_history_now(opts):
    """Reset the ledger so every growth/delta is measured from the current values.

    Like :func:`save_history_now` it performs one silent scan (implying
    ``--sudo`` and ignoring the other options/environment) and exits, but it
    *replaces* the ledger instead of merging: the current system and process
    values become the new baseline, exactly as if the tool had first run just
    after this boot.  Prints nothing on success and returns ``0``; on failure it
    prints an explanation to stderr and returns ``1``.
    """
    if os.geteuid() == 0:
        return _collect_and_save_history(opts, reset=True)
    reason = rerun_module_as_root('pmemstat.main', ['--reset-history-now'])
    print('pmemstat: history reset FAILED: could not elevate to root '
          f'({reason})', file=sys.stderr)
    return 1


def _collect_and_save_history(opts, reset=False):
    """One silent scan and ledger write; returns ``0`` on success, else ``1``.

    Every other option is ignored: the scan is forced to a non-window one-shot
    whose report is suppressed, and the ledger write is done here so its
    success/failure becomes the exit code.  When ``reset`` is set the existing
    ledger is replaced (the current values become the new baseline) rather than
    merged.
    """
    verb = 'reset' if reset else 'update'
    opts.window = False
    opts.loop_secs = 0
    opts.debug = False
    opts.cpu = False
    opts.fit_to_window = False
    if opts.min_delta_kb is None:
        opts.min_delta_kb = 100 if opts.units == 'KB' else 1000
    pmemstat = PmemStat(opts=opts)
    ProcMem.pmemstat = pmemstat
    ProcMem.opts = opts
    # The scan builds growth_samples/sys_samples; the ledger write is done here
    # so its success/failure becomes the exit code.  Suppress the scan's report.
    pmemstat.history_use = False
    pmemstat.history_save = False
    saved_stdout = sys.stdout
    try:
        with open(os.devnull, 'w', encoding='utf-8') as devnull:
            sys.stdout = devnull
            try:
                pmemstat.loop(datetime.now(), is_first=True)
            finally:
                sys.stdout = saved_stdout
    except Exception as exc:  # pylint: disable=broad-exception-caught
        print(f'pmemstat: history {verb} FAILED: {exc}', file=sys.stderr)
        return 1
    if not pmemstat.growth_samples:
        print(f'pmemstat: history {verb} FAILED: no process data collected',
              file=sys.stderr)
        return 1
    try:
        write = History.reset_ledger if reset else History.update_ledger
        ok = write(pmemstat.growth_samples, pmemstat.sys_samples,
                   boot=pmemstat.history_boot)
    except Exception as exc:  # pylint: disable=broad-exception-caught
        print(f'pmemstat: history {verb} FAILED: {exc}', file=sys.stderr)
        return 1
    if not ok:
        print(f'pmemstat: history {verb} FAILED: ledger not written '
              '(another run holds the lock, or the state dir is unwritable)',
              file=sys.stderr)
        return 1
    return 0


def _cpu_avg_secs(value):
    """Argparse type for the CPU smoothing window: an integer in ``5..90``.

    Raising ``argparse.ArgumentTypeError`` gives a clean, tailored message for
    both non-integers and out-of-range integers.
    """
    import argparse
    try:
        secs = int(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError(
            f'invalid integer: {value!r}') from None
    if not 5 <= secs <= 90:
        raise argparse.ArgumentTypeError(
            'must be between 5 and 90 seconds')
    return secs


def main():
    """Main loop"""
    global DebugLevel
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('-D', '--debug', action='count', default=0,
            help='debug mode (the more Ds, the higher the debug level)')
    parser.add_argument('-C', '--no-cpu', action='store_false', dest='cpu',
            help='do NOT report percent CPU (only in window mode)')
    parser.add_argument('-a', '--cpu-avg-secs', type=_cpu_avg_secs, default=20,
            help='CPU moving-average smoothing window in seconds [5-90, dflt=20]')
    parser.add_argument('-P', '--psi', action='store_true',
            help='show PSI (system pressure in header + memPSI%% column) [dflt=off]')
    parser.add_argument('--theme', default=None, metavar='NAME',
            help='initial colour theme (see --list-themes); "mono" disables '
                 'colour. The T picker remembers the choice.')
    parser.add_argument('--no-color', '--no-colour', action='store_true',
            dest='no_color',
            help='disable colour (the same as --theme mono)')
    parser.add_argument('--list-themes', action='store_true',
            help='list the available themes and exit')
    parser.add_argument('-g', '--groupby',
            choices=('exe', 'cmd', 'pid', 'cgroup', 'cgroupCharge'),
            default='exe',
            help='grouping method for presenting rows; the cgroup variants '
                 'differ only in the memory view: "cgroup" (footprt) or '
                 '"cgroupCharge" (memory.current)')
    parser.add_argument('-f', '--fit-to-window', action='store_true',
            help='do not overflow window [if -w]')
    parser.add_argument('-k', '--min-delta-kb', type=int, default=None,
            help='minimum delta KB to show again [dflt=100 if DB else 1000]')
    parser.add_argument('-l', '--loop', type=int, default=0, dest='loop_secs',
            help='loop interval in secs [dflt=5 if -w else 0]')
    parser.add_argument('-L', '--cmdlen', type=int, default=36,
            help='max shown command length [dflt=36 if not -w]')
    parser.add_argument('-t', '--top-pct', type=int, default=100,
            help='report group contributing to top pct of ptotal [dflt=100]')
    parser.add_argument('-U', '--user', action='store_true',
            help='run as user (NOT as root); marks the header with a dimmed '
                 '"--user" so the degraded view is obvious')
    parser.add_argument('--sudo', action='store_true',
            help='re-run self as root via sudo '
                 '(or set PMEMSTAT_ARGS=--sudo)')
    parser.add_argument('--save-history-now', action='store_true',
            help='save the current stats to the history ledger and exit '
                 '(implies --sudo; ignores other options and the environment)')
    parser.add_argument('--reset-history-now', action='store_true',
            help='reset the history ledger so all growth is measured from the '
                 'current values, then exit '
                 '(implies --sudo; ignores other options and the environment)')
    parser.add_argument('-o', '--others', action='store_false',
            help='expand "other" into shSYSV, shOth, stack, text')
    parser.add_argument('-u', '--units', choices=('MB', 'KB', 'human'),
            default='MB', help='units of memory [dflt=MB]')
    parser.add_argument('-s', '--sortby',
            choices=('mem', 'cpu', 'name', 'growth'),
            default='mem', help='sort method for presenting rows')
    parser.add_argument('--growth-style',
            choices=('off', 'both', 'growth', 'rate'), default='off',
            help='leak/growth style: off|both|growth|rate [dflt=off]')
    parser.add_argument('--growth-top',
            choices=('all', '10'), default='all',
            help='annotate only the top-N growers: all|10 [dflt=all]')
    parser.add_argument('--no-growth-history', action='store_true',
            help='do NOT seed growth from the cross-run ledger (fresh baseline)')
    parser.add_argument('--dont-save-growth-history', action='store_true',
            help='do NOT write the cross-run growth ledger')
    parser.add_argument('-/', '--search', default='',
            help='show items with search string in name')
    parser.add_argument('-W', '--no-window', action='store_false', dest='window',
            help='show in "curses" window [disables: -D,-t,-L]')
    parser.add_argument('pids', nargs='*', action='store',
            help='list of pids/groups (none means every accessible pid)')
    effective_argv, argv_error = _resolve_argv(
        sys.argv[1:], os.environ.get(ARGS_ENV, ''))
    if argv_error is not None:
        print(f'pmemstat: bad {ARGS_ENV}: {argv_error}', file=sys.stderr)
        sys.exit(2)
    opts = parser.parse_args(effective_argv)
    # DB(0, f'opts={opts}')

    if opts.list_themes:
        for name in Theme.list_all():
            print(f'{name:16} {Theme.THEMES[name]["name"]}')
        print(f'{MONO_THEME:16} No colour (bold/reverse attributes only)')
        return 0

    if opts.save_history_now:
        return save_history_now(opts)
    if opts.reset_history_now:
        return reset_history_now(opts)

    # Preferences are loaded *after* the cron-style one-shot modes, which must
    # never touch (or create) a user's config directory.
    prefs = UiPrefs()
    opts.theme, opts.colors = resolve_ui_prefs(opts, prefs)
    opts.prefs = prefs
    set_colors_enabled(opts.colors)

    if not opts.user and os.geteuid() != 0:
        if opts.sudo:
            # Opted in explicitly: elevation failure is fatal (a silent
            # downgrade scrolls off-screen and hides that the data is partial).
            _elevate_or_exit(opts, effective_argv)
        elif not opts.window:
            print("pmemstat: running as user (other users' processes omitted); "
                  'run "sudo pmemstat" or set PMEMSTAT_ARGS=--sudo for '
                  'full coverage.', file=sys.stderr)


    if opts.min_delta_kb is None:
        opts.min_delta_kb = 100 if opts.units == 'KB' else 1000
    if opts.window:
        if opts.loop_secs < 1:
            opts.loop_secs = 5
        if opts.fit_to_window:
            opts.top_pct = 100
        opts.cmdlen = 100
        opts.debug = False
        opts.top_pct = 100
    else:
        opts.fit_to_window = False
        opts.cpu = False
    DebugLevel = opts.debug
    if opts.debug:
        DB(1, 'DebugLevel', DebugLevel)

    pmemstat = PmemStat(opts=opts)
    ProcMem.pmemstat = pmemstat
    ProcMem.opts = opts

    try:
        signal.signal(signal.SIGTERM, _handle_sigterm)
    except (ValueError, OSError):  # not on the main thread / unsupported
        pass

    # History lifecycle: recover a crashed run + mark the start now, and always
    # finalize (append the closing anchor, mark a clean shutdown) on the way
    # out, including on Ctrl-C / SIGTERM / sys.exit.
    try:
        pmemstat.history_begin()
        if opts.window:
            if opts.loop_secs <= 0:
                opts.loop_secs = 5
            pmemstat.window_loop()
        else:
            is_first = True
            while True:
                now = datetime.now()
                pmemstat.loop(now, is_first)
                if opts.loop_secs <= 0:
                    break
                until_dt = now + timedelta(0, opts.loop_secs)
                diff_dt = until_dt - datetime.now()
                seconds = diff_dt.total_seconds()
                if seconds > 0:
                    time.sleep(seconds)
                is_first = False
    finally:
        pmemstat.history_finalize()
    return 0


def run():
    """Wrap main in try/except and exit with a status code."""
    try:
        code = main()
    except KeyboardInterrupt:
        code = 130
    except Exception as exce:  # pylint: disable=broad-exception-caught
        ConsoleWindow.stop_curses()
        print("exception:", str(exce))
        print(traceback.format_exc())
        code = 1
    sys.exit(code or 0)


if __name__ == '__main__':
    run()
