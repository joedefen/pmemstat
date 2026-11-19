#!/usr/bin/env python3
"""
Copyright (c) 2022-2026 Joe Defen

A program to aggregate the memory used by processes into
categories so that the memory footprint of processes is more clear.

The lines of smaps look as sequences of the lines shown below.
We call the 1st line a "section line" and those following "item lines".

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

NOTE: kB is a misnomer ...  should be "KB".  Morons.
"""

import os
import re
import shlex
import signal
import subprocess
import sys
import time
import traceback
from datetime import datetime, timedelta

# from curses.textpad import rectangle
from types import SimpleNamespace

# console-window ~= 3.1.0 is required (see pyproject.toml).
from console_window import (
    ConsoleWindow,
    IncrementalSearchBar,
    InlineConfirmation,
    OptionSpinner,
    Theme,
    resolve_appearance,
)

from pmemstat import Debug, History
from pmemstat import __version__ as PMEMSTAT_VERSION
from pmemstat.CGroup import CGroup, cgroup_leaf, has_descendant, local_values
from pmemstat.Debug import DB
from pmemstat.Growth import GrowthTracker
from pmemstat.PmemCommon import HISTORY_ENV_DISABLE, cgroup_view_totals
from pmemstat.PmemGrowth import GrowthMixin
from pmemstat.PmemKeys import KeyMixin
from pmemstat.PmemReport import ReportMixin
from pmemstat.ProcMem import ProcMem
from pmemstat.UiPrefs import UiPrefs
from pmemstat.Zram import ZramProjector

# Trace Levels: 0 - forced, temporary debugging (comment it out) 1+ - regular
# debugging (higher is less important and/or more verbose)


class PmemStat(GrowthMixin, ReportMixin, KeyMixin):
    """ The singleton class for running the main loop, etc"""

    KEY_LEGEND = ('[?]:help [g]roup [u]nits [s]ort [c]pu [K]ill [P]SI'
                  ' [G]rowth')
    KEY_LEGEND_ZAP = ' [z]ap'
    KEY_LEGEND_USER = '--user'
    OTHER_KEYS = ('shSYSV', 'shOth', 'stack', 'text')
    GROWTH_HEADER = 'growth'
    WIDTH_SHRINK_DELAY = 4
    RATE_MIN_SECS = 60
    GROWTH_PRUNE_SECS = 3600
    ZAP_CONFIRM_SECS = 10.0
    ZAP_CONFIRM_TEXT = ('Really want to reset the growth baseline to now?  '
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
        opts.kill_mode = False # pseudo option
        # Screen-model state: `needs_refresh` asks for a fresh /proc
        # sample and `regroup_pending` carries a mode/fit change into it.
        self.screens = {}
        self.stack = None
        self.prefs = getattr(opts, 'prefs', None)
        self.running = True
        self.needs_refresh = True
        self.first_loop = True
        self.regroup_pending = True
        self.regroup_from_key = False
        self._suppress_new_marker = False
        self.groups_by_line = {}
        # Persistent per-(view, key) growth trackers.
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
        # Cross-run growth ledger.  The boot id is captured once (it is stable for the life
        # of the process); growth_samples is refreshed every loop
        # by update_growth so the periodic ledger write is cheap.
        # history_use seeds the in-memory trackers from the
        # ledger's baselines; history_save writes the ledger.
        # PMEMSTAT_NO_HISTORY disables both; the CLI flags disable one each.
        no_history = bool(os.environ.get(HISTORY_ENV_DISABLE))
        self.history_use = not no_history and not getattr(
            opts, 'no_growth_history', False)
        self.history_save = not no_history and not getattr(
            opts, 'dont_save_growth_history', False)
        # The ledger holds system-wide samples,
        # which only root can produce (and only a root run's
        # coverage is a consistent sample space across runs).
        # Non-root sees just one user's processes,
        # so seeding from a root-written baseline -- or writing a
        # user-only baseline into the same per-user ledger for a later
        # root run to trust -- would mix two incompatible sample spaces.
        # Non-root therefore ignores the ledger entirely and measures growth
        # from this run's start only (the same behavior as --no-growth-history).
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
        # Deadline (monotonic) of the armed 'z' reset prompt, else None.
        # See zap_confirm_start(): 'z' only asks, 'y' resets, ESC / no answer does not.
        self.zap_confirm_deadline = None

    @property
    def win(self):
        """Alias for :attr:`window`, the name console-window's Screen expects."""
        return self.window

    def active_screen(self):
        """The Screen the window is painting, or ``None`` outside window mode."""
        return self.stack.get_curr_obj() if self.stack is not None else None

    def has_zram(self):
        """Have zRAM actually? """
        return bool(self.zram_projector and self.zram_projector.devs)

    def _search_accept(self):
        """Called when search is accepted (Enter pressed)"""
        if self.window:
            self.window.passthrough_mode = False

    def _search_cancel(self, original_text):
        """Called when search is canceled (Esc pressed)"""
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
        # Right-aligned header labels touch when a label is as wide as the column,
        # so keep the width at least one wider than the longest label (e.g.
        # "kmem"+"footprt", or "cpu%"+"memPSI%" at human width).
        label_width = max(len(name) for name in ProcMem.make_summary_dict())
        self.fwidth = max(self.fwidth, label_width + 1)

    def get_group(self, key):
        """Per group info."""
        group = self.groups.get(key, None)
        if not group:
            group = SimpleNamespace(key=key,
                    is_new=not self._suppress_new_marker,
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

        - Usually the cgroup leaf unit name (with trailing '+' if has listed descendants).
        - But systemd app scopes are often named after launcher that spawned app (e.g.
          an ``app-niri-fuzzel-*.scope`` that actually hosts VS Code),
          so when the leaf name matches none of the member executables
          the row is labeled with dominant member app instead.
        - The full cgroup path stays searchable (see the report loop).
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
        """Read the kernel's cgroup v2 numbers for a group (raw/subtree)."""
        group.summary['info'] = self.cgroup_label(group)
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
            # Record *both* totals, not just the active view's,
            # so the growth loop can reuse this single read for both cgroup groupings.
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
        """Reduce each group's cgroup columns to its own ("local") share

         * Arrgh. cgroup v2 accounting is hierarchical,
           so a parent's raw numbers include its descendant groups'.
         * Subtracting the descendants' raw values makes every row its own share and lets the
           rows sum to TOTALS; the "+" says its descendants were subtracted.
         * ``oK`` is recomputed on the local slices so the row still reconciles to the (local) total.
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



# pmemstat runs as root by default,
# * re-executing itself through sudo so it can read every process's smaps.
# * Extra default CLI arguments may be supplied once via PMEMSTAT_ARGS,
#   e.g.: export PMEMSTAT_ARGS='--psi --loop 3 -s name'
# * The value is # tokenized like a POSIX shell command line and prepended to real argv
ARGS_ENV = 'PMEMSTAT_ARGS'

def args_from_env():
    """CLI arguments supplied via ``PMEMSTAT_ARGS`` (``[]`` when unset).

    NOTE: Raises ``ValueError`` on unbalanced quotes.
    """
    return shlex.split(os.environ.get(ARGS_ENV, ''))


def resolve_ui_theme(opts, prefs, environ=None):
    """The starting theme: one value, resolved before the window exists."""
    return Theme.resolve(resolve_appearance(
        cli_theme=getattr(opts, 'theme', None),
        saved_theme=prefs.get('theme') or '',
        environ=environ))


def _resolve_argv(raw_argv, env_text):
    """Effective argv for ``main``; returns ``(argv, error)``."""
    raw_argv = list(raw_argv)
    for flag in ('--save-history-now', '--reset-history-now'):
        if flag in raw_argv:
            return [flag], None
    try:
        return shlex.split(env_text) + raw_argv, None
    except ValueError as exc:
        return None, str(exc)

def _isolated_can_import(module_root):
    """Whether an isolated interpreter (``python -I``) would find module_root."""
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

def elevation_required(opts):
    """True when this run needs root (the default, unless ``-U``)."""
    return not getattr(opts, 'user', False) and os.geteuid() != 0


def rerun_module_as_root(module_name, args=None):
    """Re-exec ``module_name`` as root via sudo, safely."""
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
    """Exit fatally when root elevation (the default) cannot happen."""
    reason = rerun_module_as_root('pmemstat.main', effective_argv)
    if reason is None:
        return  # already root; nothing to do
    print(f'pmemstat: FATAL: could not elevate to root (the default): {reason}. '
          'Run as root (e.g. "sudo pmemstat") or pass -U/--user for the '
          'user-only view.', file=sys.stderr)
    sys.exit(1)


def save_history_now(opts):
    """Collect the current stats once and write the ledger, then exit (no UI)."""
    if os.geteuid() == 0:
        return _collect_and_save_history(opts)
    reason = rerun_module_as_root('pmemstat.main', ['--save-history-now'])
    # Reaching here means elevation did not happen (success replaces the process),
    # so any return value is a failure reason.
    print('pmemstat: history update FAILED: could not elevate to root '
          f'({reason})', file=sys.stderr)
    return 1


def reset_history_now(opts):
    """Reset the ledger so every growth/delta is measured from the current values."""
    if os.geteuid() == 0:
        return _collect_and_save_history(opts, reset=True)
    reason = rerun_module_as_root('pmemstat.main', ['--reset-history-now'])
    print('pmemstat: history reset FAILED: could not elevate to root '
          f'({reason})', file=sys.stderr)
    return 1


def _collect_and_save_history(opts, reset=False):
    """One silent scan and ledger write; returns ``0`` on success, else ``1``."""
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
    # The scan builds growth_samples/sys_samples; the ledger write
    # is done here so its success/failure becomes the exit code.
    # Suppress the scan's report.
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
    except Exception as exc:
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
    except Exception as exc:
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

    Raising ``argparse.ArgumentTypeError`` gives a clean,
    tailored message for both non-integers and out-of-range integers.
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
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--version', action='version',
            version=f'%(prog)s {PMEMSTAT_VERSION}')
    parser.add_argument('-D', '--debug', action='count', default=0,
            help='debug mode (the more Ds, the higher the debug level)')
    parser.add_argument('-C', '--no-cpu', action='store_false', dest='cpu',
            help='do NOT report percent CPU (only in window mode)')
    parser.add_argument('-a', '--cpu-avg-secs', type=_cpu_avg_secs, default=20,
            help='CPU moving-average smoothing window in seconds [5-90, dflt=20]')
    parser.add_argument('-P', '--psi', action='store_true',
            help='show PSI (system pressure in header + memPSI%% column) [dflt=off]')
    parser.add_argument('--theme', default=None, metavar='NAME',
            help='initial color theme (see --list-themes); "mono" disables '
                 'color (it is also the last entry in the T picker).')
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
            help='opt out of the default root elevation and run as user; '
                 'marks the header with a dimmed "--user" so the degraded '
                 'view is obvious')
    # Accepted for compatibility with the release that made elevation opt-in
    parser.add_argument('--sudo', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--save-history-now', action='store_true',
            help='save the current stats for growth tracking and exit '
                 '(runs as root; ignores other options and the environment)')
    parser.add_argument('--reset-history-now', action='store_true',
            help='reset the saved growth history so all growth is measured '
                 'from the current values, then exit '
                 '(runs as root; ignores other options and the environment)')
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
            help='do NOT seed growth from the saved history (fresh baseline)')
    parser.add_argument('--dont-save-growth-history', action='store_true',
            help='do NOT update the saved growth history')
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
        return 0

    if opts.save_history_now:
        return save_history_now(opts)
    if opts.reset_history_now:
        return reset_history_now(opts)

    # Preferences are loaded *after* the cron-style one-shot modes,
    # which must never touch (or create) a user's config directory.
    prefs = UiPrefs()
    # One value: an explicit --theme, then NO_COLOR (mono), then the saved choice.
    # The window constructor applies it (Theme.set_active),
    # so the color switch agrees with the first paint without a second call here.
    opts.theme = resolve_ui_theme(opts, prefs)
    opts.prefs = prefs

    # Root is the default view; only -U/--user opts out.
    # Elevation failure is fatal (a silent downgrade scrolls
    # off-screen and hides that the data is partial).
    if elevation_required(opts):
        _elevate_or_exit(opts, effective_argv)


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
    Debug.DebugLevel = opts.debug
    if opts.debug:
        DB(1, 'DebugLevel', Debug.DebugLevel)

    pmemstat = PmemStat(opts=opts)
    ProcMem.pmemstat = pmemstat
    ProcMem.opts = opts

    try:
        signal.signal(signal.SIGTERM, _handle_sigterm)
    except (ValueError, OSError):  # not on the main thread / unsupported
        pass

    # History lifecycle: recover a crashed run + mark the start now,
    # and always finalize (append the closing anchor, mark a clean shutdown) on the way out,
    # including on Ctrl-C / SIGTERM / sys.exit.
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
    except Exception as exce:
        # The class-level stop_curses() only exists for singleton mode and
        # raises otherwise; stop_all() is the permanent process-wide teardown.
        ConsoleWindow.stop_all()
        print("exception:", str(exce))
        print(traceback.format_exc())
        code = 1
    sys.exit(code or 0)


if __name__ == '__main__':
    run()
