"""Growth, history, and zap-confirmation methods for PmemStat."""
import curses
import os
import time
from datetime import datetime, timezone

from console_window import Glyphs, Theme, semantic_attr

from pmemstat import History
from pmemstat.CGroup import CGroup, local_values
from pmemstat.Debug import DB
from pmemstat.Format import ago_str, human_kb
from pmemstat.Growth import GrowthTracker, seed_from_baselines, seed_sys_from_baselines
from pmemstat.PmemCommon import GROWTH_VIEWS, cgroup_view_totals

# Methods live here but run on the composed PmemStat instance.


class GrowthMixin:
    def suppresses_new_marker(self, regroup):
        """True when a sample's re-created rows must not show the 'A' marker.

        A grouping change clears ``self.groups``,
        so every row is re-created as "new" and would show an 'A' for exactly one refresh.
        That is fine when growth is off (it explains the reshuffle),
        but in a growth mode it merely hides the annotations the user switched grouping to
        watch: the per-view trackers survive the change and already hold the values to show,
        so annotate with those instead.
        The initial frame is not a grouping change (``regroup_from_key`` is False),
        so its markers stay; a key that genuinely
        appears on a later sample still gets its 'A'.
        """
        return bool(regroup and self.regroup_from_key
                    and getattr(self.opts, 'growth_style', 'off') != 'off')

    def prep_new_loop(self, regroup):
        """Prepare for a new loop.  Returns whether or not any groups are left.
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
        """Scalar used for loop filtering and growth ("total incl.  swap").

        Under zRAM the rollup ``ptotal`` already contains
        ``SwapPss`` (see :meth:`ProcMem.parse_rollups`),
        so ``pswap`` must not be added again; without zRAM it must be.
        This keeps the metric correct in both cases and fixes a latent double count.
        """
        return summary['pss'] + (0 if self.has_zram() else summary['pswap'])

    def test_delta(self, group, summary, o_summary):
        """Check whether the group rollup or smaps summary exceeds threshold """
        is_over = False
        # DB(0, f'{group.key} o=[{group.o_summary}]\n          n=[{group.summary}]')
        delta_pss = (self.growth_metric_value(summary)
                     - self.growth_metric_value(o_summary))
        thresh = self.opts.min_delta_kb

        # DB(0, f'{group.key} ~pss {delta_pss}KB min={self.opts.min_delta_kb}') DB(0,
        # f'{group.key} ~pss {delta_pss}KB thresh={thresh}')
        if ((thresh <= 0 and abs(delta_pss) >= -thresh)
                or (thresh > 0 and delta_pss >= thresh)):
            is_over = True
            if self.debug:
                DB(2, f'{group.key} ~pss {delta_pss}KB thresh={thresh}')
        return is_over, delta_pss

    def prc_growth_metric(self, prc):
        """Per-process scalar fed to the non-cgroup view trackers (KB).

        Uses the rollup total including swap (zRAM-aware),
        i.e. the same metric the non-cgroup rows display.
        """
        return self.growth_metric_value(prc.rollup)

    def cgroup_growth_samples(self, samples):
        """Override the cgroup views' samples with kernel charge totals.

        Reuses the reads already done for an active cgroup grouping
        when possible; otherwise reads each distinct cgroup once
        (both cgroup groupings are fed from the single read).
        Values are reduced to each cgroup's own ("local")
        share so they match the displayed rows.
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

         * Trackers are keyed by ``(view, key)`` and live on the singleton,
            so the leak history is continuous from the start
            of the tool no matter how the grouping is switched.
         * Every view is fed each loop, not just the active one: the non-cgroup views share
            a single pass over the per-process rollups (cheap)
         * The cgroup views share one read per distinct cgroup.
         * Trackers run unconditionally (even when the growth annotation
           is ``off``) so growth data is always ready to view.
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
        """Merge the latest samples into the ledger (throttled unless forced)."""
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
        """Runtime equivalent of ``--reset-history-now`` (the ``z`` key)."""
        if os.geteuid() != 0:
            return False
        for tracker in self.growth_trackers.values():
            tracker.reset()
        for tracker in self.sys_trackers.values():
            tracker.reset()
        self.growth_results = {}
        self.sys_growth = {}
        # The ledger baselines are being replaced,
        # so never re-seed the (now fresh) in-memory trackers from a stale ledger.
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
        """Arm the ``z`` confirmation; nothing is reset until it is answered."""
        self.zap_confirm_deadline = time.monotonic() + self.ZAP_CONFIRM_SECS

    def zap_confirm_cancel(self):
        """Withdraw the ``z`` prompt without resetting anything."""
        self.zap_confirm_deadline = None

    def zap_confirm_text(self):
        """The prompt line shown while the ``z`` reset awaits an answer."""
        remaining = int(round(self.zap_confirm_remaining()))
        return f'{Glyphs.get("star_glow")} {self.ZAP_CONFIRM_TEXT}  [{remaining}s]'

    def zap_confirm_handle_key(self, key):
        """Answer the ``z`` prompt: ``y`` confirms, ESC declines."""
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
            return 'canceled'
        return 'continue'

    def handle_zap_confirm_key(self, key):
        """Feed ``key`` to the ``z`` prompt, resetting only on a ``y`` answer.

        Returns True when the prompt is (or was) active,
        i.e. the key belonged to it and must not reach the normal key handlers.
        """
        if not self.zap_confirm_active():
            return False
        result = self.zap_confirm_handle_key(key)
        if result == 'continue':
            return True
        if result == 'confirmed':
            msg = ('Growth history zapped' if self.zap_history()
                   else 'Growth baseline reset (saved history unchanged)')
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

        Returns True when a prompt was withdrawn.
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
        """Minimum width of the lead (growth) column."""
        if getattr(self.opts, 'growth_style', 'off') == 'off':
            return 1
        return len(self.GROWTH_HEADER)

    def growth_lead_header(self):
        """Header fragment for the lead (growth) column, including its pad."""
        if getattr(self.opts, 'growth_style', 'off') == 'off':
            return ' ' * (self.lead_width + 1)
        return f' {self.GROWTH_HEADER:>{self.lead_width}}'

    def growth_lead_blank(self):
        """Blank lead field for the column-header line."""
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

        Returns ``(width, new_count)``.
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
        """One line: system growth for Used/TOTALS/Sh+Tmp and the residual."""
        return ''.join(text for text, _ in self.sys_growth_segments())

    def sys_growth_segments(self):
        """The system growth line as ``(text, attr)`` runs."""
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
        text, attr = segments[-1]
        segments[-1] = (text.rstrip(), attr)
        if self.opts.growth_style != 'off':
            result = next((self.sys_growth.get(key) for key in keys
                           if self.sys_growth.get(key)), None)
            if result:
                segments.append((f' [{ago_str(result[1])}]', curses.A_DIM))
        return [(text, attr) for text, attr in segments if text]
