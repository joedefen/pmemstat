#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Pending Features:
  - Ability to kill processes?
  - Search string
  - Improved CPU stat (keep list of values for 20s or s)
  - Iffy:
    - PSI Indicator in title?  PSI Page (maybe with a thread)?

Keys:
    a
    b
 n  C-b PAGE-UP - page up
    c: cpu: ON/off
    d
 n  C-d Half page down
    e
    f: fit: ON/off
 n  C-f PAGE-DOWN - page down
    g: groupby: exe/cmd/pid
    h : enter/exit help page
    H HOME:  goto top of list
    i
 n  j DOWN - one line down
 n  k UP - one line up
    l
    m
    n: line numbers: ON/off
    o: collapse cols to "others": ON/off
    p
    q
    r: rise to the top: ON/off
    s
    t
    u units: MB/mB/KB/human
 n  C-u Half page up
    v
    w
    x
    y
    z
    $ END: go to end of list




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

import os
import re
import sys
import traceback
import time
import subprocess
import textwrap
import curses
# from curses.textpad import rectangle
from types import SimpleNamespace
from io import StringIO
from datetime import datetime, timedelta
from Window import Window


# Trace Levels:
#  0 - forced, temporary debugging (comment it out)
#  1+ - regular debugging (higher is less important and/or more verbose)

DebugLevel = 0

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

##############################################################################
##   human()
##############################################################################
def human(number):
    """ Return a concise number description."""
    suffixes = ['K', 'M', 'G', 'T']
    while suffixes:
        suffix = suffixes.pop(0)
        number /= 1024
        if number < 999.95 or not suffixes:
            return f'{number:.1f}{suffix}'

######
####################################################################################
######
######
####################################################################################
######

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
            r'^(THPeligible|VmFlags)'
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
    max_cmd_len = 32 # command line maximum length
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

    def __init__(self, pid):
        self.pid = pid
        self.alive = True
        self.is_new = True
        self.wanted = True # until proven otherwise
        self.is_changed = False
        self.whynot = None # populate me with why unwanted
        self.smaps_file = f'/proc/{self.pid}/smaps'
        self.rollup_file = f'/proc/{self.pid}/smaps_rollup'
        self.rollup_lines = []
        self.smaps_lines = []
        self.chunks = []
        self.cpu = None
        self.exebasename = None, None
        self.key, self.cmdline, self.cmdline_trunc = None, None, None

    def refresh_cpu(self):
        """Get the Cpu Number for the PID (if possible)"""
        def init_cpu(error=False):
            return SimpleNamespace(error=error, fh=None,
                base_mono=0, base_ticks=0, percent=0,)

        if ProcMem.clock_tick is None:
            try:
                ProcMem.clock_tick = 0
                rv = subprocess.run(['getconf', 'CLK_TCK'],
                            capture_output=True, text=True, check=True)
                ProcMem.clock_tick = int(rv.stdout.strip())
            except Exception:
                pass
            if self.clock_tick <= 0:
                ProcMem.clock_tick = 100

        if not self.cpu:
            self.cpu = init_cpu()
            try:
                # pylint: disable=consider-using-with
                self.cpu.fh = open(f'/proc/{self.pid}/stat', encoding='utf-8')
            except (PermissionError, FileNotFoundError):
                self.cpu = init_cpu(error=True)
                return
        if self.cpu.error:
            return
        try:
            self.cpu.fh.seek(0)
            data = self.cpu.fh.read().split()
            user, system = int(data[13]), int(data[14])
        except Exception:
            self.cpu = init_cpu(error=True)
            return
        ticks = user + system
        mono = time.monotonic()

        if self.cpu.base_mono >= 0: # initialized
            delta_ticks = ticks - self.cpu.base_ticks
            delta_time = mono - self.cpu.base_mono
            self.cpu.percent = 0
            if delta_time > 0 and delta_ticks >= 0:
                self.cpu.percent = round(100
                    * delta_ticks / ProcMem.clock_tick / delta_time, 8)
            # print(f'{self.cpu.percent}%')
            if delta_time >= 20: # rebase sample
                self.cpu.base_mono = mono
                self.cpu.base_ticks = ticks
        else:
            self.cpu.base_mono = mono
            self.cpu.base_ticks = ticks

    def get_cmdline(self):
        """Get the command line of the PID."""
        try:

            cmdline_file = f'/proc/{self.pid}/cmdline'
            try:
                # pylint: disable=consider-using-with
                line = open(cmdline_file, encoding='utf-8').read()[:-1]
            except FileNotFoundError as exc:
                # this seems to be a race which ignore; either the process is just
                # started or just quickly ended before even identified
                if DebugLevel:
                    DB(1, f'skip pid={self.pid} no-rollup-lines exc={type(exc).__name__}')
                return
            arguments = line.split('\0')
            if not arguments or not arguments[0]: # kernel process
                self.wanted = False
                self.whynot = 'KernelProcess'
                # print(f'{self.pid}: kernel thread')
                return
            # DB(0, f'{self.pid}: {arguments}')
            # sometimes the first word
            wds = os.path.basename(arguments[0]).split() + arguments[1:]
            basename = re.sub(r'^\W+', '', wds.pop(0))
            basename = re.sub(r'\W+$', '', basename)
            # DB(0, f'basename={basename} wds={wds}')
            if basename in ('python', 'python2', 'python3', 'perl', 'bash', 'ruby',
                    'sh', 'ksh', 'zsh') and wds:
                script = os.path.basename(wds[0])
                # DB(0, f'script={script} wds[0]={wds[0]}')
                if script != wds[0]:
                    basename = f'{basename}->{script}'
                    del wds[0]
            self.exebasename = basename
            self.cmdline = ' '.join([basename] + wds)
            self.cmdline_trunc = self.cmdline[0:ProcMem.max_cmd_len]
            # DB(0, f'basename={basename} cmdline_trunc={self.cmdline}')
        except Exception as exc:
            # not really expecting this ... probably a bug
            print(f'  WARNING: skip pid={self.pid} no-basename exc={exc}')
            print(traceback.format_exc())
            self.wanted = False
            self.whynot = 'CannotGetCmdline'
            return

        ## print(f'DBDB: {self.pid} {ProcMem.opts.pids}')
        # filter unwanted before too much work
        if (ProcMem.opts.pids and str(self.pid) not in ProcMem.opts.pids
                and self.exebasename not in ProcMem.opts.pids):
            self.wanted = False
            self.whynot = 'FilteredByArgs'
            # print(f'    >>>> unwanted {pid}')
        ## print(f'DBDB: {self.pid} wanted={self.wanted} whynot={self.whynot}')
        self.key = (self.cmdline_trunc if ProcMem.opts.groupby == 'cmd' else
                self.exebasename if ProcMem.opts.groupby == 'exe' else self.pid)

    def read_lines(self, filename):
        """ Get the lines of the smaps """
        lines = None
        try:
            with open(filename, encoding='utf-8') as fhandle:
                lines = fhandle.read().splitlines()
        except (PermissionError, FileNotFoundError) as exc:
            # normal cases: not permitted or this is a race where the pid is terminating
            self.whynot = f'CannotReadLines({type(exc).__name__})'
        except Exception as exc:
            # unexpected cases (probably a bug)
            if not self.opts.window:
                print(f'ERROR: skip pid={self.pid}',
                      f'no-smaps-or-rollup-lines exc={type(exc).__name__}')
            self.whynot = f'CannotReadLines({type(exc).__name__})'
        return lines

    def get_rollup_lines(self):
        """Get the lines of the 'smaps_rollup' file for this PID"""
        self.rollup_lines = []
        try:
            self.rollup_lines = self.read_lines(self.rollup_file)
        except Exception as exc:
            self.rollup_lines = []
            if DebugLevel:
                DB(1, f'skip pid={self.pid} no-rollup-lines exc={type(exc).__name__}')

        if not self.rollup_lines:
            self.wanted = False
            self.whynot = 'CannotReadRollups'
        elif DebugLevel:
            DB(3, f'pid={self.pid} {self.exebasename} #rollup_lines={len(self.rollup_lines)}')

        return bool(self.rollup_lines)

    def get_smaps_lines(self):
        """Get the lines of the 'smaps' file for this PID"""
        self.smaps_lines = []
        try:
            self.smaps_lines = self.read_lines(self.smaps_file)
        except Exception as exc:
            self.smaps_lines = []
            if DebugLevel:
                DB(1, f'skip pid={self.pid} no-smap-lines exc={type(exc).__name__}')

        if not self.smaps_lines:
            self.wanted = False
            self.whynot = 'CannotReadSmaps'
        else:
            if DebugLevel:
                DB(1, f'pid={self.pid} {self.exebasename} #smaps_lines={len(self.smaps_lines)}')
        return bool(self.smaps_lines)

    def make_chunks(self, lines):
        """ Parse the already smaps read lines."""
        self.chunks = []
        chunk = None
        for idx, line in enumerate(lines):
            match = self.section_pat.match(line)
            if match:
                if chunk:
                    self.chunks.append(chunk)
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
            print(f'ERROR: cannot parse "{line}" [{self.smaps_file}:{idx+1}]')
        if chunk:
            self.chunks.append(chunk)

    @staticmethod
    def make_summary_dict(pid=0, info=''):
        """ Make an object to summarize memory use of a PID or group """
        summary = {
                'cpu_pct': 0,
                'pswap': 0,
                'shSYSV': 0,
                'shOth': 0, # e.g., memory mapped file
                'stack': 0,
                'text': 0,
                'data': 0, # deprecated 'pseudo' (e.g., memory barrier) now in 'data'
                'ptotal': 0,
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
                continue
            print(f'ERROR: cannot parse "{line}" [{self.rollup_file}:{idx+1}]')
        summary['pss'] = summary['ptotal'] # for consistency
        return summary

    def categorize_chunks(self):
        """ Analyze the chunks to categorize the memory """
        for idx, chunk in enumerate(self.chunks):
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
            elif (chunk.size == 4 and idx < len(self.chunks) - 1
                    and chunk.offset == chunk.beg and not chunk.item
                    and '---p' in chunk.perms):
                    # stack seems to be 4K unwriteable immediately followed
                    # by something very huge like 10240 or 10236.
                    # The size is bogus ... replace the 'Size' with
                    # the 'Private' plus swapped
                nchunk = self.chunks[idx+1]
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
            for chunk in self.chunks:
                DB(6, '{self.pid} {self.exebasename} CHUNK:', chunk)

    def summarize_chunks(self):
        """ Accumulate the chunks into the summary of memory use for the PID """
        summary = self.make_summary_dict(self.pid)

        for chunk in self.chunks:
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
        if not self.whynot and not self.cmdline:
            self.get_cmdline()
            if not self.cmdline:
                return
        if not self.whynot:
            self.get_rollup_lines()
        if self.whynot:
            DB(4, f'pid={self.pid} {self.exebasename} whynot={self.whynot}')
            return
        self.is_changed = False
        rollup_summary = self.parse_rollups(self.rollup_lines)
        if self.opts.cpu:
            self.refresh_cpu()
            rollup_summary['cpu_pct'] = self.cpu.percent
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
    keys_we_handle =  set([ord('c'), ord('f'), ord('g'), ord('o'),
                    ord('u'), ord('n'), ord('h'), ord('?'),
                    ord('s'), ord('r'), ord('/'), ord('?'),
                    curses.KEY_ENTER, 10,
                      ])

    def __init__(self, opts):
        self.opts = opts
        self.loop_num = 0
        self.debug = opts.debug
        self.prcs = {}
        self.groups = {} # indexed by group key (e.g., cmd)
        self.window = None
        self.number = 0  # line number for opts.numbers
        self.units, self.divisor, self.fwidth = 0, 0, 0
        self.mode = 'normal' # (or 'help' or ?'psi')
        self._set_units()

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
        if self.units == 'mB':
            self.divisor = 1000*1000
            self.fwidth = 8
        elif self.units == 'MB':
            self.divisor = 1024*1024
            self.fwidth = 8
        elif self.units == 'KB':
            self.divisor = 1024 # KB (the original)
            self.fwidth = 11
        else: # human
            self.divisor = 1 # human
            self.fwidth = 7

    def get_group(self, key):
        """Per group info."""
        group = self.groups.get(key, None)
        if not group:
            group = SimpleNamespace(key=key,
                    is_new=True,
                    alive=False,
                    whynot=None,
                    changed=False,
                    o_prcset=set(),
                    prcset=set(),
                    o_rollup_summary=None,
                    rollup_summary=None,
                    o_summary=None,
                    summary=None,
                    first_summary=None,
                    growth_pct=0.0)
            self.groups[key] = group
            # DB(0, f'add group[{key}]')
        return group

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
                if key in ('info',):
                    pass
                elif key in ('number',):
                    total[key] += 1 if val <= 0 else val
                elif isinstance(val, (int, float)):
                    total[key] += val

    def test_delta(self, group, summary, o_summary):
        """Check whether the group rollup or smaps summary exceeds threshold """
        # pylint: disable=chained-comparison
        is_over = False
        # DB(0, f'{group.key} o=[{group.o_summary}]\n          n=[{group.summary}]')
        delta_pss = summary['pss'] - o_summary['pss'] + summary['pswap'] - o_summary['pswap']
        thresh = self.opts.min_delta_kb

        # DB(0, f'{group.key} ~pss {delta_pss}KB min={self.opts.min_delta_kb}')
        # DB(0, f'{group.key} ~pss {delta_pss}KB thresh={thresh}')
        if ((thresh <= 0 and abs(delta_pss) >= -thresh)
                or (thresh > 0 and delta_pss >= thresh)):
            is_over = True
            if self.debug:
                DB(2, f'{group.key} ~pss {delta_pss}KB thresh={thresh}')
        return is_over, delta_pss

    def prc_group(self, group):
        """Process on group"""
        do_smaps = False
        if group.o_rollup_summary:
            do_smaps, _ = self.test_delta(
                    group, group.rollup_summary, group.o_rollup_summary)
        else:
            do_smaps = True

        for prc in list(group.prcset):
            group.summary['info'] = (f'{prc.exebasename}' if self.opts.groupby == 'exe'
                    else f'{prc.cmdline_trunc}' if self.opts.groupby == 'cmd'
                    else f'{prc.pid} {prc.cmdline_trunc}')
            if do_smaps:
                prc.get_smaps_lines()
                if prc.whynot:
                    group.prcset.remove(prc)
                    continue
                prc.make_chunks(prc.smaps_lines)
                prc.categorize_chunks()
                summary = prc.summarize_chunks()
                self.add_to_summary(summary, group.summary)
        group.summary['pss'] = group.rollup_summary['ptotal']
        group.summary['pswap'] = group.rollup_summary['pswap']
        group.summary['cpu_pct'] = group.rollup_summary['cpu_pct']

        if not group.prcset:
            group.alive = False
            do_smaps = False
        if not do_smaps:
            group.summary = group.o_summary
            if group.summary and group.rollup_summary:
                group.summary['cpu_pct'] = group.rollup_summary['cpu_pct']
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

    def pr_exclusions(self):
        """ TBD """
        exclusions = {'number', 'info'}
        if not self.opts.cpu:
            exclusions.add('cpu_pct')
        others = ['text', 'shSYSV', 'shOth', 'stack'] if self.opts.others else []
        if not self.debug:
            exclusions.add('pss')
        return others, exclusions

    def pr_summary(self, lead, summary, attr=None, to_head=False):
        """Print a summary of memory use"""
        body = ''
        others, exclusions = self.pr_exclusions()
        others_mb = 0
        if self.opts.numbers:
            body += f'{self.number:>4}'
        self.number += 1
        for item, value in summary.items():
            if item not in exclusions:
                if item in ('cpu_pct', ):
                    body += f'{value:>{self.fwidth}.1f}'
                    continue
                mbytes = int(round(value*1024/self.divisor))
                if item in others:
                    others_mb += mbytes
                    if item != others[0]:
                        continue
                    mbytes = others_mb
                if self.divisor > 1:
                    body += f'{mbytes:>{self.fwidth},}'
                else:
                    body += f'{human(mbytes):>{self.fwidth}}'
        num = summary['number']
        self.emit(f'{body} {lead} '
                  + (f'{-num}' if num <= 0 else f'{num}x')
                  + ' ' + summary['info'], attr=attr, to_head=to_head)

    @staticmethod
    def get_meminfo():
        """Get most vital stats from /proc/meminfo'"""
        meminfofile = '/proc/meminfo'
        meminfoKB = {'MemTotal': 0, 'MemAvailable': 0, 'Dirty':0}
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

    def loop(self, now, is_first, regroup=False):
        """one loop thru all pids"""

        def pr_top_of_report():
            nonlocal self, meminfoKB, wanted_prcs, total_pids
            # print timestamp of report
            if not self.window:
                leader = f'\n---- {now.strftime("%H:%M:%S")}'
            else:
                leader = f'{now.strftime("%H:%M:%S")}'
                self.emit(leader, to_head=True,
                          attr=curses.A_BOLD if self.loop_num % 2 else None)
                leader = ''

            leader += f' Mem={human(meminfoKB["MemTotal"]*1024)}'
            leader += f' Avail={human(meminfoKB["MemAvailable"]*1024)}'
            leader += f' Dirty={human(meminfoKB["Dirty"]*1024)}'
            leader += f' PIDs: {len(wanted_prcs)}/{total_pids}'
            self.emit(leader, to_head=True, resume=bool(self.window))
            if self.opts.search:
                self.emit(' /', to_head=True, resume=True)
                self.emit(self.opts.search, to_head=True,
                          resume=True, attr=curses.A_UNDERLINE)
                self.emit('/', to_head=True, resume=True)

            # pylint: disable=too-many-branches
        self.loop_num += 1
        meminfoKB = self.get_meminfo()
        total_pids = 0
        allpids = []
        wanted_prcs = {}

        self.prep_new_loop(regroup)

        if self.window and (is_first or regroup):
            pr_top_of_report()
            self.emit('   WORKING .... be patient ;-)', attr=curses.A_REVERSE)
            self.emit('   HINTS:')
            self.emit('     - Type "h" to enter Help Screen')
            self.emit('     - Type "Ctrl-C" to exit program')
            if os.geteuid != 0:
                self.emit('     - Run with "sudo" to show all PIDs!',
                          attr=curses.A_BOLD)

            self.window.render()
            self.window.clear()

        with os.scandir('/proc') as it:
            for entry in it:
                # if re.match(r'^\d+$', entry.name):
                if entry.name.isdigit():
                    allpids.append(entry.name)

        for pid in allpids:
            ## print(f'DBDB pid={pid} self.opts.pids={opts.pids}')
            prc = self.prcs.get(pid, None)
            if not prc:
                prc = ProcMem(int(pid))
                self.prcs[pid] = prc
            else:
                prc.is_new = False
            prc.prc_pid()
            ## if str(pid) in opts.pids:
                ## print(f'DBDB pid={pid} dir={vars(prc)}')
            total_pids += 0 if prc.whynot == 'KernelProcess' else 1

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
        for group in self.groups.values():
            if group.alive:
                self.prc_group(group)
                self.add_to_summary(group.summary, grand_summary)

        # detect changed group on basis of differing PIDs contributing

        if grand_summary['number'] == 0:
            print('DONE: no pids to report ... exiting now')
            sys.exit(0)

        # print header and  grand totals
        pr_top_of_report()

        header = ''
        others, exclusions = self.pr_exclusions()
        self.number = 0
        if self.opts.numbers:
            header += '   #'
        for item in grand_summary:
            if item not in exclusions:
                if item in others:
                    if item != others[0]:
                        continue
                    item = 'other'
                header += f'{item:>{self.fwidth}}'
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
            sorted_keys = sorted(alive_groups.keys(),
                key=lambda x: alive_groups[x].summary['cpu_pct'], reverse=True)
        elif self.get_sortby() == 'name':
            sorted_keys = sorted(alive_groups.keys(),
                key=lambda x: str(alive_groups[x].key))
        else:
            sorted_keys = sorted(alive_groups.keys(),
                key=lambda x: (alive_groups[x].is_changed and self.opts.rise_to_top,
                               alive_groups[x].summary['ptotal']), reverse=True)

        limit = self.window.max_body_count if self.is_fit_opted() else 1000000
        ptotal_limit = (grand_summary['ptotal'] * self.opts.top_pct / 100) * 1.001
        others_summary = None
        running_summary = ProcMem.make_summary_dict(info='---- RUNNING ----')
        shown_cnt = 0
        for key in sorted_keys:
            group = alive_groups[key]
            self.add_to_summary(group.summary, running_summary)
            if (self.opts.search in group.summary['info'] and
              shown_cnt < limit-1 and running_summary['ptotal'] <= ptotal_limit):
                if group.alive and (group.is_new or group.is_changed or self.window):
                    attr = curses.A_REVERSE if group.is_new or group.is_changed else None
                    attr = None if is_first else attr
                    self.pr_summary('A' if group.is_new
                        else f'{group.delta_pss:+,}K' if group.is_changed
                        else ' ', group.summary, attr=attr)
                    shown_cnt += 1
                    # DB(0, f'obj: {vars(obj)}')
            elif is_first or self.opts.window:
                if not others_summary:
                    others_summary = ProcMem.make_summary_dict(info='---- OTHERS ----')
                self.add_to_summary(group.summary, others_summary)
        if others_summary:
            self.pr_summary('O',  others_summary)

        remainder = limit - self.window.body_count if self.is_fit_opted() else 1000000
        for group in self.groups.values():
            if not group.alive and group.o_summary and remainder > 0:
                remainder -= 1
                self.pr_summary('x', group.o_summary)

    def emit(self, line, to_head=False, attr=None, resume=False):
        """ Emit a line of the report"""
        if self.window:
            if to_head:
                self.window.add_header(line, attr=attr, resume=resume)
                self.window.calc()
            else:
                self.window.add_body(line, attr=attr, resume=resume)
        else:
            print(line)

    def help_screen(self):
        """Populate help screen"""
        headers = """
            -- HELP SCREEN ['h' or ENTER return; Ctrl-c exits] --
            Navigation:
                k, UP:  up one row             H, HOME:  top row
              j, DOWN:  down one row            $, END:  bottom row
               Ctrl-u:  half-page up     Ctrl-b, PPAGE:  page up
               Ctrl-d:  half-page down   Ctrl-f, NPAGE:  page down
            Type option keys below to rotate choice:
        """
        for line in textwrap.dedent(headers).splitlines():
            self.emit(line, to_head=True)

        options = [
                ['c - show cpu', 'cpu', 'ON', 'off'],
                ['f - fit rows to window', 'fit_to_window', 'ON', 'off'],
                ['g - group by', 'groupby', 'exe', 'cmd', 'pid'],
                ['n - line numbers', 'numbers',  'ON', 'off'],
                ['o - less memory detail', 'others',  'ON', 'off'],
                ['r - raise new/changed to top', 'rise_to_top',  'ON', 'off'],
                ['s - sort by', 'sortby', 'mem', 'cpu', 'name'],
                ['u - memory units', 'units', 'MB', 'mB', 'KB', 'human'],
                ['/ - search string', 'search', self.opts.search],
        ]
        for option in options:
            leader = option[0]
            member = option[1]
            choices = option[2:]
            value = getattr(self.opts, member)
            if isinstance(value, bool):
                value = "ON" if value else "off"
            self.emit(f'{leader:>30}: ')
            for choice in choices:
                self.emit(' ', resume=True)
                self.emit(f'{choice}', resume=True,
                    attr=curses.A_REVERSE if choice == value else None)

    def window_loop(self):
        """ TBD """
        def do_key(key):
            regroup = False
            # ENSURE keys are in 'keys_we_handle'
            if key in (ord('c'), ):
                self.opts.cpu = not self.opts.cpu
            elif key in (ord('f'), ):
                self.opts.fit_to_window = not self.opts.fit_to_window
            elif key in (ord('g'), ):
                after = {'exe': 'cmd', 'cmd': 'pid', 'pid': 'exe'}
                self.opts.groupby = after[self.opts.groupby]
                regroup = True
            elif key in (ord('n'), ):
                self.opts.numbers = not self.opts.numbers
            elif key in (ord('o'), ):
                self.opts.others = not self.opts.others
            elif key in (ord('r'), ):
                self.opts.rise_to_top = not self.opts.rise_to_top
            elif key in (ord('s'), ):
                after = {'mem': 'cpu', 'cpu': 'name', 'name': 'mem'}
                self.opts.sortby = after[self.opts.sortby]
            elif key in (ord('u'), ):
                after = {'MB': 'mB', 'mB': 'KB', 'KB': 'human', 'human': 'MB'}
                self.opts.units = after[self.opts.units]
                self._set_units()
            elif key in (ord('h'), ord('?')):
                after = {'normal': 'help', 'help': 'normal'}
                self.mode = after[self.mode]
            elif key in (curses.KEY_ENTER, 10):
                if self.mode == 'help':
                    self.mode = 'normal'
            elif key in (ord('/'), ):
                self.opts.search = self.window.answer(
                    prompt='Set search string, then Enter',
                    seed=self.opts.search)

            return regroup

        self.window = Window(head_line=True, keys=self.keys_we_handle)
        is_first = True
        was_groupby, regroup = self.opts.groupby, True
        for _ in range(1000000000):
            if self.mode == 'normal':
                regroup = bool(was_groupby != self.opts.groupby)
                self.loop(datetime.now(), is_first=is_first, regroup=regroup)
                was_groupby, regroup = self.opts.groupby, False
                regroup = False
                self.window.render()
                do_key(self.window.prompt(self.opts.loop_secs))
                self.window.clear()
                is_first = False
            elif self.mode == 'help':
                self.help_screen()
                self.window.render()
                do_key(self.window.prompt(self.opts.loop_secs))
                self.window.clear()
            else:
                assert False, f'unsupported mode ({self.mode})'

def main():
    """Main loop"""
    global DebugLevel
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('-D', '--debug', action='count', default=0,
            help='debug mode (the more Ds, the higher the debug level)')
    parser.add_argument('-C', '--no-cpu', action='store_false', dest='cpu',
            help='do NOT report percent CPU (only in window mode)')
    parser.add_argument('-g', '--groupby', choices=('exe', 'cmd', 'pid'),
            default='exe', help='grouping method for presenting rows')
    parser.add_argument('-f', '--fit-to-window', action='store_true',
            help='do not overflow window [if -w]')
    parser.add_argument('-k', '--min-delta-kb', type=int, default=None,
            help='minimum delta KB to show again [dflt=100 if DB else 1000')
    parser.add_argument('-l', '--loop', type=int, default=0, dest='loop_secs',
            help='loop interval in secs [dflt=5 if -w else 0]')
    parser.add_argument('-L', '--cmdlen', type=int, default=36,
            help='max shown command length [dflt=36 if not -w]')
    parser.add_argument('-t', '--top-pct', type=int, default=100,
            help='report group contributing to top pct of ptotal [dflt=100]')
    parser.add_argument('-n', '--numbers', action='store_true',
            help='show line numbers in report')
    parser.add_argument('-o', '--others', action='store_true',
            help='collapse shSYSV, shOth, stack, text into "other"')
    parser.add_argument('-u', '--units', choices=('MB', 'mB', 'KB', 'human'),
            default='MB', help='units of memory [dflt=MB]')
    parser.add_argument('-R', '--no-rise', action='store_false', dest='rise_to_top',
            help='do NOT raise change/adds to top (only in window mode)')
    parser.add_argument('-s', '--sortby', choices=('mem', 'cpu', 'name'),
            default='mem', help='grouping method for presenting rows')
    parser.add_argument('-/', '--search', 
            help='show items with search string in name')
    parser.add_argument('-W', '--no-window', action='store_false', dest='window',
            help='show in "curses" window [disables: -D,-t,-L]')
    parser.add_argument('pids', nargs='*', action='store',
            help='list of pids/groups (none means every accessible pid)')
    opts = parser.parse_args()
    # DB(0, f'opts={opts}')

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


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        pass
    except Exception as exce:
        Window.stop_curses()
        print("exception:", str(exce))
        print(traceback.format_exc())
