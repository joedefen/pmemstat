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
from pmemstat.KillThem import KillThem
from pmemstat.CpuSmooth import CpuSmooth, SysStat
from pmemstat import History
from pmemstat.CGroup import (CGroup, cgroup_leaf, has_descendant,
                             local_values, parse_cgroup_lines)
from pmemstat.Pressure import format_pressure_lines, read_system_pressure
# console-window is intentionally pinned to an exact version (see pyproject.toml).
# Provide a clear, actionable message instead of a bare ImportError when the
# pinned package is missing or when a different version has been substituted.
_CONSOLE_WINDOW_PIN = '1.4.3'
try:
    import console_window
    from console_window import (ConsoleWindow, OptionSpinner,
                                IncrementalSearchBar, InlineConfirmation)
except ImportError as _cw_exc:  # pragma: no cover (only on a broken install)
    raise SystemExit(
        'pmemstat requires the console-window package pinned to '
        f'version {_CONSOLE_WINDOW_PIN} (an intentional exact pin by the author).\n'
        f'Could not import console_window: {_cw_exc}\n'
        'Install/repair it with:\n'
        f"    python3 -m pip install --user 'console-window=={_CONSOLE_WINDOW_PIN}'"
    ) from _cw_exc

_cw_found = getattr(console_window, '__version__', None)
if _cw_found is not None and _cw_found != _CONSOLE_WINDOW_PIN:  # pragma: no cover
    raise SystemExit(
        'pmemstat requires console-window=='
        f'{_CONSOLE_WINDOW_PIN} but found {_cw_found}.\n'
        'The exact pin is intentional. Reinstall the pinned version with:\n'
        f"    python3 -m pip install --user 'console-window=={_CONSOLE_WINDOW_PIN}'"
    )

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
        """Forget all history (used on regroup)."""
        self.t0 = None
        self.peak = None
        self.snaps = []
        self._next_age = self.BASE
        self.last_seen = None

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
    (``Used``/``TOTALS``/``ShTmp``/``OthK``/``OthU``): ``trackers`` and
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
                # pylint: disable=consider-using-with
                line = open(cmdline_file, encoding='utf-8').read()[:-1]
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
    KEY_LEGEND = ('[?]help [g]roup [u]nits [s]ort [c]pu [K]ill [p]SI'
                  ' [G]rowth [t]op')
    # PSS categories folded into the single combined "other" column when -o is
    # not given; the first entry is the rendered slot (the sum is shown there).
    OTHER_KEYS = ('shSYSV', 'shOth', 'stack', 'text')
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
        self.mode = 'normal' # (or 'help' or ?'psi')
        setattr(opts, 'kill_mode', False) # pseudo option
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
            'Used', 'TOTALS', 'ShTmp', 'OthK', 'OthU')}
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
                 if now - tracker.last_seen > self.GROWTH_PRUNE_SECS]
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
        """One line: system growth for Used/TOTALS/Sh+Tmp/OthK/OthU.

        Values (non-negative, so no ``+``) are left-justified to a common width
        for the current mode.  The shared baseline interval is shown once, as a
        trailing ``[interval]`` in every growth mode (``both``/``growth``/
        ``rate``).  The brackets make it clear that the age applies to all five
        deltas (all five trackers share one baseline) rather than looking like
        an attribute of the last (``OthU``) field, and it gives the context
        needed to judge the numbers -- in particular a ``rate`` projection,
        which is otherwise silent about the window it extrapolates from.
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

        keys = ('Used', 'TOTALS', 'ShTmp', 'OthK', 'OthU')
        values = {key: value_of(key) for key in keys}
        width = max(len(text) for text in values.values())
        self.sys_growth_width, self._sysgrow_small = self._debounced_width(
            self.sys_growth_width, width, self._sysgrow_small)
        line = '    ' + '  '.join(
            f'Δ{key} {values[key]:<{self.sys_growth_width}}' for key in keys)
        if self.opts.growth_style != 'off':
            result = next((self.sys_growth.get(key) for key in keys
                           if self.sys_growth.get(key)), None)
            if result:
                line = line.rstrip() + f' [{ago_str(result[1])}]'
        return line.rstrip()

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
        """Print a summary of memory use (columns from view_columns())."""
        body = ''
        combined = bool(self.opts.others)
        for item in self.view_columns():
            if not item:
                body += '  '
                continue
            value = summary.get(item, 0)
            if value is None:
                body += f'{"n/a":>{self.fwidth}}'
                continue
            if item in ('cpu%', 'memPSI%'):
                body += f'{value:>{self.fwidth}.1f}'
                continue
            if combined and item == self.OTHER_KEYS[0]:
                value = sum(summary.get(key, 0) for key in self.OTHER_KEYS)
            mbytes = int(round(value*1024/self.divisor))
            if self.divisor > 1:
                body += f'{mbytes:>{self.fwidth},}'
            else:
                body += f'{human(mbytes):>{self.fwidth}}'
        num = summary['number']
        lead = f'{lead:<{self.lead_width}}'
        self.emit(f'{body} {lead} '
                  + (f'{-num}' if num <= 0 else f'{num}x')
                  + ' ' + summary['info'], attr=attr, to_head=to_head)

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
            othk = (meminfoKB["SUnreclaim"] + meminfoKB["KernelStack"]
                    + meminfoKB["PageTables"])
            if appKB:
                other = (meminfoKB["MemTotal"] - appKB
                         - meminfoKB["MemAvailable"] - meminfoKB["Shmem"])
                leader += f' OthK={human(othk*1024)}'
                leader += f' OthU={human((other-othk)*1024)}'
            elif othk:
                leader += f' OthK={human(othk*1024)}'
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
                self.emit(self.format_sys_growth(), to_head=True, resume=False)
                resume = True

            if self.has_zram(): # second line if zRAM
                resume = False
                proj = self.zram_projector
                if self.opts.cpu:
                    leader = f'{kernel_cpu:8.1f}%/ker '
                    self.emit(leader, to_head=True, resume=resume)
                    resume = True
                self.emit(f'MajF/s={major_fault_rate} ', to_head=True, resume=resume)
                resume = True
                self.emit(f' zRAM={human(self.zram_projector.meminfo.MemZram)}',
                          to_head=True, attr=curses.A_BOLD, resume=resume)
                leader = ''
                leader += f' CR={proj.ratio:.1f}'
                leader += f' eTot:{proj.human_pct(proj.e_max_used)}'
                leader += f' eUsed:{proj.human_pct(proj.e_used)}'
                leader += f' eAvail:{proj.human_pct(proj.e_avail)}'
                # leader += f' Dirty={human(meminfoKB["Dirty"]*1024)}'
                self.emit(leader, to_head=True, resume=resume)
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
                    attr = curses.A_BOLD if idx == 0 else None
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
                    total_kernel_pids += 1
                    self.kernel_prcs.append(prc)
                else:
                    total_user_pids += 1

        for prc in prcs:
            prc.prc_pid()
            pid = prc.pid
            ## if str(pid) in opts.pids:
                ## print(f'DBDB pid={pid} dir={vars(prc)}')

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
        need = 1
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
        self.emit(f'{header}   key/info'
                + f' ({self.opts.groupby} by {self.get_sortby()})',
                to_head=True, attr=curses.A_BOLD)
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
                        self.emit(prompt, attr=curses.A_REVERSE)
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
        """Emit the key legend (first header line) plus the live search field.

        The legend is left-aligned so '[?]help' sits above the first digit of
        the leader's 'Tot=' value; the search field is appended after it and is
        drawn non-dimmed to signal that it is live. No-op outside window mode,
        where there are no interactive keys.
        """
        if not self.window:
            return
        # 'HH:MM:SS Tot=' is the leader prefix; offset the legend under the
        # first digit of the Tot= value.
        pad = len('00:00:00 Tot=')
        self.emit(f'{" " * pad}{self.KEY_LEGEND} ', to_head=True,
                  attr=curses.A_DIM, resume=False)
        if self.search_bar.is_active:
            before = self.search_bar.text[:self.search_bar.cursor_pos]
            after = self.search_bar.text[self.search_bar.cursor_pos:]
            self.emit(f'/{before}|{after}', to_head=True,
                      attr=curses.A_REVERSE, resume=True)
        else:
            self.emit(f'/{self.opts.search or ""}', to_head=True,
                      attr=curses.A_BOLD, resume=True)

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

    def help_screen(self):
        """Populate help screen"""
        self.emit("-- HELP SCREEN ['?' or ENTER closes Help; Ctrl-C exits ] --",
                   to_head=True, attr=curses.A_BOLD)
        self.spin.show_help_nav_keys(self.window)
        if os.geteuid() != 0:
            self.emit('Hint: run "sudo pmemstat" to show all PIDs',
                       attr=curses.A_BOLD)
        self.spin.show_help_body(self.window)

    def window_loop(self):
        """ TBD """
        def do_key(key):
            regroup = False
            # ENSURE keys are in 'keys_we_handle'
            if key in (ord('/'), ):
                # Start inline search mode
                self.search_bar.start(self.opts.search)
                self.window.passthrough_mode = True
                return regroup
            if key in self.spin.keys:
                self.spin.do_key(key, self.window)
                if key in (ord('u'), ):
                    self._set_units()
                elif key in (ord('?'), ):
                    self.window.set_pick_mode(False if self.mode == 'help'
                                           else self.opts.kill_mode)
                elif key in (ord('K'), ):
                    if self.mode == 'normal':
                        self.window.set_pick_mode(self.opts.kill_mode)

            elif key in (curses.KEY_ENTER, 10):
                if self.mode == 'help':
                    self.mode = 'normal'
                elif self.opts.kill_mode:
                    win = self.window
                    group = self.groups_by_line.get(win.pick_pos, None)
                    if group:
                        pids = [x.pid for x in group.prcset]
                        # Start inline confirmation (requires 'y' key to confirm)
                        pid_count = len(pids)
                        pid_label = f'[{pid_count} {"PID" if pid_count == 1 else "PIDs"}]'
                        self.confirmation.start(
                            action_type='kill',
                            identity=f'{group.summary["info"]} {pid_label}',
                            mode='y'
                        )
                        win.passthrough_mode = True
                    else:
                        # No group selected, exit kill mode
                        self.opts.kill_mode = False
                        self.window.set_pick_mode(self.opts.kill_mode)
            return regroup

        self.spin = OptionSpinner()
        self.spin.add_key('mode', '? - help screen',
                          vals=['normal', 'help'], obj=self)
        self.spin.add_key('kill_mode', 'K - kill mode', vals=[False, True],
                comments='Select line + ENTER to kill selected', obj=self.opts)
        self.spin.add_key('fit_to_window', 'f - fit rows to window',
                          vals=[False, True], obj=self.opts)
        self.spin.add_key('groupby', 'g - group by',
                          vals=['exe', 'cmd', 'pid', 'cgroup',
                                'cgroupCharge'], obj=self.opts)
        self.spin.add_key('others', 'o - less category detail',
                          vals=[False, True], obj=self.opts)
        self.spin.add_key('sortby', 's - sort by',
                          vals=['mem', 'cpu', 'name', 'growth'], obj=self.opts)
        # NB: 'L' is a console_window navigation key (end-of-page), so use 'G'.
        self.spin.add_key('growth_style', 'G - leak/growth mode',
                          vals=['off', 'both', 'growth', 'rate'], obj=self.opts)
        self.spin.add_key('growth_top', 't - top-N growers',
                          vals=['all', '10'], obj=self.opts)
        self.spin.add_key('units', 'u - memory units',
                          vals=['MB', 'KB', 'human'], obj=self.opts)
        self.spin.add_key('cpu', 'c - show cpu',
                          vals=[False, True], obj=self.opts)
        self.spin.add_key('psi', 'p - show PSI (header + memPSI%)',
                          vals=[False, True], obj=self.opts)

        keys_we_handle =  [ord('K'), ord('/'), 27, curses.KEY_ENTER, 10] + list(self.spin.keys)
        self.window = ConsoleWindow(head_line=True, keys=keys_we_handle)
        is_first = True
        was_groupby, regroup = self.opts.groupby, True
        was_others = self.opts.others
        for _ in range(1000000000):
            if self.mode == 'help':
                self.window.set_pick_mode(False)
                self.help_screen()
                self.window.render()
                key = self.window.prompt(self.opts.loop_secs)
                if key is not None:
                    do_key(key)
                self.window.clear()
            elif self.mode == 'normal':
                regroup = bool(was_groupby != self.opts.groupby)
                if not regroup:
                    regroup = bool(was_others != self.opts.others)
                self.loop(datetime.now(), is_first=is_first, regroup=regroup)
                was_groupby, was_others = self.opts.groupby, self.opts.others
                regroup = False
                self.window.set_pick_mode(self.opts.kill_mode)
                self.window.render()
                key = self.window.prompt(self.opts.loop_secs)
                if key is None:
                    # prompt() timed out (refresh interval): redraw only; never
                    # pass None into the key handlers (search bar compares it).
                    self.window.clear()
                    is_first = False
                    continue
                # Let confirmation handle keys when active (highest priority)
                enter_kill_loop = True
                if self.confirmation.active:
                    result = self.confirmation.handle_key(key)
                    if result == 'confirmed':
                        # User confirmed - execute the kill
                        group = self.groups_by_line.get(self.window.pick_pos, None)
                        if group:
                            pids = [x.pid for x in group.prcset]
                            killer = KillThem(pids)
                            ok, message = killer.do_kill()
                            # Flash result message for 1 second
                            self.window.flash(f'{"✓" if ok else "✗"} {message}', duration=1.0)
                        self.confirmation.cancel()
                        self.window.passthrough_mode = False
                        self.opts.kill_mode = False
                        self.window.set_pick_mode(self.opts.kill_mode)
                    elif result == 'cancelled':
                        # User cancelled with ESC - go back to outer loop to redisplay
                        self.confirmation.cancel()
                        self.window.passthrough_mode = False
                        enter_kill_loop = False  # Skip kill loop to trigger redisplay
                # Let search bar handle keys when active
                elif self.search_bar.is_active and self.search_bar.handle_key(key):
                    pass  # Key was handled by search bar
                else:
                    do_key(key)
                if enter_kill_loop:
                    while self.opts.kill_mode and not self.confirmation.active:
                        if self.mode == 'help':
                            break
                        self.window.render()
                        key = self.window.prompt(self.opts.loop_secs)
                        if key is None:
                            continue
                        # Let search bar handle keys when active
                        if self.search_bar.is_active and self.search_bar.handle_key(key):
                            pass  # Key was handled by search bar
                        else:
                            do_key(key)
                            # If confirmation was just started, break to outer loop to show prompt
                            if self.confirmation.active:
                                break
                self.window.clear()
                is_first = False
            else:
                assert False, f'unsupported mode ({self.mode})'

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
    parser.add_argument('-U', '--run-as-user', action='store_true',
            help='run as user (NOT as root)')
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

    if opts.save_history_now:
        return save_history_now(opts)
    if opts.reset_history_now:
        return reset_history_now(opts)

    if not opts.run_as_user and os.geteuid() != 0:
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
