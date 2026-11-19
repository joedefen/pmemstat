"""Report collection and rendering methods for PmemStat."""
import curses
import os
import re
import sys
import time
from types import SimpleNamespace

from console_window import Theme, semantic_attr

from pmemstat.CGroup import cgroup_leaf
from pmemstat.CpuSmooth import SysStat
from pmemstat.Debug import DB
from pmemstat.Format import human
from pmemstat.PmemCommon import is_cgroup_groupby
from pmemstat.Pressure import format_pressure_lines, read_system_pressure
from pmemstat.ProcMem import ProcMem

# Methods live here but run on the composed PmemStat instance.

read_smaps = 0


class ReportMixin:
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
        """Active cgroup memory view: ``'footprt'`` or ``'kcharge'``."""
        return ('kcharge' if getattr(self.opts, 'groupby', None) == 'cgroupCharge'
                else 'footprt')

    def total_key(self):
        """Column key holding the current view's memory total."""
        if self.is_cgroup() and self.cgroup_data_seen:
            return self.cgroup_view()
        return 'ptotal'

    def view_columns(self):
        """Ordered column keys rendered for the current mode/view."""
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
        segments = self.summary_segments(lead, summary)
        if attr is not None:
            # A caller-supplied whole-row attribute wins: collapse
            # the runs so its style is not mixed with the role colors.
            self.emit_segments([(''.join(text for text, _ in segments), attr)],
                               to_head=to_head)
        else:
            self.emit_segments(segments, to_head=to_head)

    def summary_segments(self, lead, summary):
        """One summary row as ``(text, attr)`` runs (see :meth:`pr_summary`)."""
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
        """Color role for a summary row's inline growth/marker column."""
        marker = lead.strip()
        if marker == 'A':
            return semantic_attr(Theme.SUCCESS)
        if marker == 'T':
            return semantic_attr(Theme.EMPHASIS)
        if marker in ('O', 'x'):
            return curses.A_DIM
        if marker:
            return semantic_attr(Theme.HOTSWAP) # growth annotation
        return None

    def info_attr(self, lead):
        """Color role for the grouping name at the end of a summary row."""
        marker = lead.strip()
        if marker in ('O', 'x'):
            return curses.A_DIM
        if marker == 'T':
            return semantic_attr(Theme.EMPHASIS)
        return None

    def lead_segment(self, lead):
        """The lead (growth/marker) cell of a summary row, plus its pad."""
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
        """The leader line's unaccounted-memory field(s)."""
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
            self.emit_key_legend()
            if self.zap_confirm_active():
                self.emit(self.zap_confirm_text(), to_head=True,
                          attr=curses.A_BOLD | semantic_attr(Theme.DANGER))
            resume = False
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

            if self.opts.growth_style != 'off' and self.sys_growth:
                self.emit_segments(self.sys_growth_segments(),
                                   to_head=True, resume=False)
                resume = True

            if self.has_zram(): # second line if zRAM
                proj = self.zram_projector
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

        self._suppress_new_marker = self.suppresses_new_marker(regroup)
        self.regroup_from_key = False
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
            self.active_screen().clear()

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
            # Count only *after* classification; prc_pid() -> get_cmdline()
            # is what sets prc.kernel for kernel threads (empty cmdline).
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

        # all pids have been processed into groups.  for each group, if it has changed,
        # sum all the smaps for the group if the group
        # rollup_summary indicates enough change
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
        # cgroup v2 stats are hierarchical; reduce by kids' share so summable
        if self.is_cgroup():
            self.apply_cgroup_locals()
            grand_summary['memPSI%'] = None
        for group in self.groups.values():
            if group.alive:
                self.add_to_summary(group.summary, grand_summary)

        if grand_summary['number'] == 0:
            print('DONE: no pids to report ... exiting now')
            sys.exit(0)

        # Feed growth trackers *always* and pick which rows may annotate.
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
        # Reserve room for the 'growth' header whenever the annotation is on
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
                        current_row = self.active_screen().body.row_cnt
                        self.groups_by_line[current_row] = group
                    lead = 'A' if group.is_new else group.growth_txt
                    self.pr_summary(lead, group.summary, attr=attr)
                    # Show confirmation prompt right after the selected line
                    if (self.window and self.confirmation.active
                            and current_row == self.active_screen().pick_pos):
                        prompt = f'  Type "y" to kill: {self.confirmation.identity} (ESC to cancel)'
                        if self.confirmation.input_buffer:
                            prompt += f' [{self.confirmation.input_buffer}_]'
                        # DANGER rather than reverse-video: the row
                        # under the pick is already reversed,
                        # and a kill prompt should not look like another picked row.
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

        remainder = (limit - self.active_screen().body.row_cnt
                     if self.is_fit_opted() else 1000000)
        for group in self.groups.values():
            if not group.alive and group.o_summary and remainder > 0:
                remainder -= 1
                self.pr_summary('x', group.o_summary)
        if not self.window:
            self.emit('')

    def emit_key_legend(self):
        """Emit the key legend (first header line) with the live search field."""
        screen = self.active_screen()
        if not self.window or screen is None:
            return
        pad = max(0, len('00:00:00 Tot=') - 5)
        legend = self.KEY_LEGEND
        if self.zap_key_offered():
            legend += self.KEY_LEGEND_ZAP
        if os.geteuid() == 0:
            lead = ' ' * pad
        else: # if crippled (non-root) run, stamp the header '--user'
            lead = self.KEY_LEGEND_USER.ljust(pad)
        if self.search_bar.is_active:
            before = self.search_bar.text[:self.search_bar.cursor_pos]
            after = self.search_bar.text[self.search_bar.cursor_pos:]
            search, search_attr = f'/{before}|{after}', curses.A_REVERSE
        else:
            search, search_attr = f'/{self.opts.search or ""}', curses.A_BOLD
        screen.add_fancy_header(f'{lead}{legend} {search}',
                                mode='Underline',
                                search_pattern_attr=search_attr,
                                line_attr=curses.A_DIM)
        self.window.calc()

    def zap_key_offered(self):
        """Whether '[z]ap' belongs in the key legend."""
        return (os.geteuid() == 0
                and getattr(self.opts, 'growth_style', 'off') != 'off')

    def emit_segments(self, segments, to_head=False, resume=False):
        """Emit one row built from ``(text, attr)`` runs."""
        segments = [(text, attr) for text, attr in segments if text]
        if not segments:
            return
        if not self.window:
            # The one-shot report has no attributes at all: keep its
            # output byte-identical by printing the joined row once.
            self.emit(''.join(text for text, _ in segments),
                      to_head=to_head, resume=resume)
            return
        for idx, (text, attr) in enumerate(segments):
            self.emit(text, to_head=to_head, attr=attr,
                      resume=bool(resume) or idx > 0)

    def emit(self, line, to_head=False, attr=None, resume=False):
        """Emit a line of the report"""
        screen = self.active_screen() if self.window else None
        if screen is not None:
            if to_head:
                screen.add_header(line, attr=attr, resume=resume)
                self.window.calc()
            else:
                screen.add_body(line, attr=attr, resume=resume)
        else:
            print(('' if resume else '\n') + line, end='')
