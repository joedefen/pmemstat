#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Class for fetching CPU usage from processes.
"""
# pylint: disable=invalid-name,broad-exception-caught,too-many-instance-attributes
# pylint: disable=import-outside-toplevel,too-many-branches,too-many-locals
# pylint: disable=consider-using-with,too-many-statements
import os
import time
import re
import traceback
import math
from types import SimpleNamespace

class Term:
    """ Escape sequences; e.g., see:
     - https://en.wikipedia.org/wiki/ANSI_escape_code
     - https://gist.github.com/fnky/458719343aabd01cfb17a3a4f7296797#file-ansi-md
    """
    esc = '\x1B'
    # pylint: disable=missing-function-docstring,multiple-statements
    @staticmethod
    def erase_line(): return f'{Term.esc}[2K'
    @staticmethod
    def erase_to_eol(): return f'{Term.esc}[0K'
    @staticmethod
    def bold(): return f'{Term.esc}[1m'
    @staticmethod
    def reverse_video(): return f'{Term.esc}[7m'
    @staticmethod
    def normal_video(): return f'{Term.esc}[m'
    @staticmethod
    def pos_up(cnt): return f'{Term.esc}[{cnt}F' if cnt > 0 else ''
    @staticmethod
    def pos_down(cnt): return f'{Term.esc}[{cnt}E' if cnt > 0 else ''
    @staticmethod
    def col(pos): return f'{Term.esc}[{pos}G'
    @staticmethod
    def clear_screen(): return f'{Term.esc}[H{Term.esc}[2J{Term.esc}[3J'

class SysStat:
    """ TBD """
    singleton = None
    def __init__(self):
        self.fh = None
        self.prev = None
        self.delta = None
        self.clock_tick = 100
        assert not self.singleton, 'cannot instantiate two SysStat'
        SysStat.singleton = self
        self._set_clock_tick()
        self._refresh()

    @staticmethod
    def get_singleton():
        """ Return THE SysStat object"""
        return SysStat.singleton if SysStat.singleton else SysStat()

    def _set_clock_tick(self):
        if self.clock_tick is None:
            try:
                self.clock_tick = os.sysconf(os.sysconf_names['SC_CLK_TCK'])
            except Exception:
                pass
            if self.clock_tick <= 0: # fake it
                self.clock_tick = 100

    @staticmethod
    def refresh():
        """ TBD """
        # pylint: disable=protected-access
        return SysStat.get_singleton()._refresh()

    def _refresh(self):
        """ TBD """
        if not self.fh:
            pathname = '/proc/stat'
            self.fh = open(pathname, "r", encoding='utf-8')
        self.fh.seek(0)

        ns = SimpleNamespace(mono=time.monotonic(),
                    cpu_cnt=0, percent=0, ticks=0)
        delta = SimpleNamespace(**vars(ns))

        for line in self.fh:
            wds = line.split()
            keyword = wds[0]
            if keyword == 'cpu':
                ns.ticks = int(wds[1])
                ns.ticks += int(wds[3])
                # needed for VM to normalize the %cpu per process
                ns.gross_ticks = sum(int(val) for val in wds[1:])
            elif keyword.startswith('cpu'):
                ns.cpu_cnt += 1

        if self.prev:
            prev = self.prev
            delta.mono = round(ns.mono - prev.mono, 4)
            delta.ticks = ns.ticks - prev.ticks
            delta.cpu_cnt = ns.cpu_cnt
            delta.max_ticks = math.ceil(self.clock_tick
                                * delta.mono * ns.cpu_cnt)
            delta.gross_ticks = ns.gross_ticks - prev.gross_ticks
            if delta.mono > 0:
                delta.percent = round(100
                    * delta.ticks / self.clock_tick / delta.mono, 4)
        self.prev = ns
        self.delta = delta
        return delta


class CpuSmooth:
    """Class that get smoothed CPU percent of given process"""
    clock_tick = None  # number of clock ticks/sec
    prev_system_stats = None
    prev_system_delta = None

    def __init__(self, pid, avg_secs=0, error=False, DB=False):
        self.pid = pid
        self.error = error # once an error stop beating head against wall
        self.DB = DB
        self.db_info = ''  # describe last update
        self.avg_secs = avg_secs # smoothing interval
        self.fh = None
        self.stat_ns = None # last read status
        self.percent = 0 # smoothed percent
        self.hists = []
        self.nickname = '' # crudely fetched on demand (for test)
        self.sys_stat = SysStat.get_singleton()

    def __del__(self):
        if self.fh:
            try:
                self.fh.close()
            except Exception:
                pass

    def _set_error(self):
        if self.fh:
            try:
                self.fh.close()
            except Exception:
                pass
            self.fh = None
        self.percent, self.error = 0, True
        return self.percent

    def get_nickname(self):
        """ Get the nickname of the process (crude)."""
        if self.nickname:
            return self.nickname
        cmdline_file = f'/proc/{self.pid}/cmdline'
        arguments, nickname = [], ''
        try:
            # pylint: disable=consider-using-with
            with open(cmdline_file, encoding='utf-8') as fh:
                for line in fh:
                    arguments = line.split('\0')
                    break
        except Exception:
            pass
        if arguments:
            wds = os.path.basename(arguments[0]).split() + arguments[1:]
            nickname = re.sub(r'^\W+', '', wds.pop(0))
            nickname = re.sub(r'\W+$', '', nickname)
            # DB(0, f'basename={basename} wds={wds}')
            if nickname in ('python', 'python2', 'python3', 'perl', 'bash', 'ruby',
                    'sh', 'ksh', 'zsh') and wds:
                script = os.path.basename(wds[0])
                # DB(0, f'script={script} wds[0]={wds[0]}')
                if script != wds[0]:
                    nickname = f'{nickname}->{script}'
        if not nickname:
            ns = self.stat_ns if self.stat_ns else self._get_stat()
            if ns and ns.exec:
                nickname = ns.exec
        self.nickname = nickname
        return nickname

    def _get_stat(self):
        if not self.fh:
            try:
                # pylint: disable=consider-using-with
                self.fh = open(f'/proc/{self.pid}/stat', encoding='utf-8')
            except (PermissionError, FileNotFoundError):
                return self._set_error()
        try:
            self.fh.seek(0)
            data = self.fh.read().split()
            self.stat_ns = SimpleNamespace(exec=data[1],
                             user=int(data[13]), system=int(data[14]),
                             nthr=int(data[19]))
        except Exception:
            self._set_error()
        return self.stat_ns

    def refresh_cpu(self):
        """Get the Cpu Number for the PID (if possible)"""
        def pct(hist0, hist1):
            delta_ticks = abs(hist0[0] - hist1[0])
            delta_mono = abs(hist0[1] - hist1[1])
            delta_gross_ticks = abs(hist0[2] - hist1[2])

            percent = 0
            cpu_cnt = self.sys_stat.prev.cpu_cnt
            if delta_gross_ticks > 0:
                percent = cpu_cnt * 100 * delta_ticks / delta_gross_ticks

            return percent, delta_ticks, delta_mono
        def pct_str(triple):
            return f'{triple[0]:7.3f}%,{triple[1]:5d},{triple[2]:7.4f}s'
#       def adjust_down(delta_ticks):
#           """ Lower current ticks by amount ... meaning add to all
#               previous ticks"""
#           for hist in self.hists[:-1]:
#               hist[0] += delta_ticks

        if self.error or not self._get_stat():
            return self.percent
        ticks = self.stat_ns.user + self.stat_ns.system
        mono = time.monotonic()
        gross_ticks = self.sys_stat.prev.gross_ticks
        self.hists.append([ticks, mono, gross_ticks])

        if len(self.hists) < 2: # takes two to tango
            return 0
        floor_mono = mono - self.avg_secs
        while len(self.hists) > 2 and self.hists[0][1] < floor_mono:
            self.hists.pop(0)
        try:
            _, _, delta_mono = pct(self.hists[-1], self.hists[-2])
        except Exception:
            pass
        if delta_mono <= 0.0:
            self.hists.pop()
            return 0

        self.percent, _, _ = pct(self.hists[-1], self.hists[0])

        # print(f'{self.percent}%')
        if self.DB:
            deltas = []
            hists = self.hists
            for idx in range(len(hists)-1, 0, -1):
                hist, prev = hists[idx], hists[idx-1]
                deltas.append(f'{hist[0]-prev[0]}'
                               + f'/{hist[1]-prev[1]:.2f}'
                               # + ('' if hist[2] <= 1 else f'#{hist[2]}')
                               )
            self.db_info = ' '
            if len(hists) >= 2:
                self.db_info = ' '.join([
                      f'{self.get_nickname()[:16]:>16} {self.pid:>6d}',
                      pct_str(pct(self.hists[-1], self.hists[-2])),
                      '//', pct_str(pct(self.hists[-1], self.hists[0])),
                        ' '.join(deltas)])
        # if self.nickname == 'firefox':
            # print(self.nickname, self.hists)
        return self.percent

if __name__ == '__main__':
    def main():
        """Main loop"""
        import argparse
        parser = argparse.ArgumentParser()
        parser.add_argument('-t', '--top', type=int, default=10,
                help='number of top CPU entries to show')
        parser.add_argument('-o', '--only', type=str, default='',
                help='name must have only substring')
        parser.add_argument('-l', '--loop', type=float, default=1.0, dest='loop_secs',
                        help='loop interval in secs [dflt=1.0]')
        opts = parser.parse_args()

        loop_secs = opts.loop_secs if opts.loop_secs >= 0.25 else 0.25
        loop_secs = opts.loop_secs if opts.loop_secs <= 120.0 else 120.0

        spots = [] # where to put the top items
        cpus = {}
        losers = set()
        pids = set()
        start_mono = time.monotonic()
        sys_stat = SysStat.get_singleton()
        while True:
            with os.scandir('/proc') as it:
                for entry in it:
                    # if re.match(r'^\d+$', entry.name):
                    if entry.name.isdigit():
                        pids.add(int(entry.name))
            old_losers, losers = losers, set()
            sys_stat.refresh()
            for pid in pids:
                if pid in old_losers:
                    losers.add(pid)
                    continue
                cpu = cpus.get(pid, None)
                if not cpu:
                    cpu = CpuSmooth(pid=pid, avg_secs=10, DB=True)
                    if not cpu.error:
                        # print(f'adding: {pid} {cpu.get_nickname()}')
                        cpus[pid] = cpu
                if cpu.error:
                    losers.add(pid)
                    continue
                cpu.refresh_cpu()
            top_cpus = sorted(cpus.values(), key=lambda x: x.percent, reverse=True)
            top_cpus = top_cpus[:opts.top]
            top_cpus = sorted(top_cpus, key=lambda x: x.pid)
            run_time = time.monotonic()-start_mono
            total_pct = 0
            for cpu in cpus.values():
                total_pct += cpu.percent
            print(f'--------- {run_time:6.1f}s {total_pct:7.2f}% ----------- ',
                  f'{sys_stat.delta}' + Term.erase_to_eol())
            old_spots, spots, todo = spots, [None]*opts.top, set()
            for cpu in top_cpus:
                try:
                    idx = old_spots.index(cpu)
                    spots[idx] = cpu
                except Exception:
                    todo.add(cpu)
            for idx, cpu in enumerate(spots):
                if not cpu:
                    cpu = todo.pop()
                    spots[idx] = cpu
                print(cpu.db_info + Term.erase_to_eol())

            time.sleep(loop_secs)
            up_one = Term.pos_up(1) + '\r'
            print(up_one * (2+len(top_cpus)))

    try:
        main()
    except KeyboardInterrupt:
        pass
    except Exception as exce:
        print("exception:", str(exce))
        print(traceback.format_exc())
