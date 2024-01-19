#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Class for fetching CPU usage from processes.
"""
# pylint: disable=invalid-name,broad-exception-caught,too-many-instance-attributes
import os
import time
import re
import traceback
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


class CpuSmooth:
    """Class that get smoothed CPU percent of given process"""
    clock_tick = None  # number of clock ticks/sec

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
        self._set_clock_tick()

    def __del__(self):
        if self.fh:
            try:
                self.fh.close()
            except Exception:
                pass

    def _set_clock_tick(self):
        if CpuSmooth.clock_tick is None:
            try:
                CpuSmooth.clock_tick = os.sysconf(os.sysconf_names['SC_CLK_TCK'])
            except Exception as exc:
                if self.DB:
                    print('DB: cannot get SC_CLK_TCK:', repr(exc))
            if self.clock_tick <= 0: # fake it
                CpuSmooth.clock_tick = 100

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
                             user=int(data[13]), system=int(data[14]))
        except Exception:
            self._set_error()
        return self.stat_ns

    def refresh_cpu(self):
        """Get the Cpu Number for the PID (if possible)"""
        def pct(hist0, hist1):
            delta_ticks = abs(hist0[0] - hist1[0])
            delta_mono = abs(hist0[1] - hist1[1])
            percent = 0
            if delta_mono > 0:
                percent = round(100
                    * delta_ticks / self.clock_tick / delta_mono, 8)
            return percent, delta_ticks, delta_mono
        def pct_str(triple):
            return f'{triple[0]:7.3f}%,{triple[1]:5d},{triple[2]:7.4}s'



        if self.error or not self._get_stat():
            return self.percent
        ticks = self.stat_ns.user + self.stat_ns.system
        mono = time.monotonic()

        if not self.hists: # not initialized, takes two to tango
            self.hists.append((ticks, mono))
            return 0
        self.hists.append((ticks, mono))
        floor_mono = mono - self.avg_secs
        while len(self.hists) > 1 and self.hists[0][1] < floor_mono:
            self.hists.pop(0)
        self.percent, _, _ = pct(self.hists[-1], self.hists[0])
        # print(f'{self.percent}%')
        if self.DB:
            self.db_info = ' '.join([
                  f'{self.get_nickname()[:16]:>16} {self.pid:>6d}',
                  pct_str(pct(self.hists[-1], self.hists[-2])),
                  '//', pct_str(pct(self.hists[-1], self.hists[0]))])
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
        
        
        cpus = {}
        losers = set()
        pids = set()
        start_mono = time.monotonic()
        while True:
            with os.scandir('/proc') as it:
                for entry in it:
                    # if re.match(r'^\d+$', entry.name):
                    if entry.name.isdigit():
                        pids.add(int(entry.name))
            old_losers, losers = losers, set()
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
            run_time = f'{time.monotonic()-start_mono:.3f}'
            print(f'--------- {run_time}----------- ')
            for cpu in top_cpus:
                print(cpu.db_info)

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
