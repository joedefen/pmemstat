"""Key routing, kill confirmation, and window loop for PmemStat."""
import curses
import os

from console_window import (
    ConsoleWindow,
    ConsoleWindowOpts,
    Glyphs,
    OptionSpinner,
    ScreenStack,
)

from pmemstat.KillThem import KillThem
from pmemstat.Screens import (
    HELP_ST,
    MAIN_ST,
    SCREEN_NAMES,
    THEMES_ST,
    HelpScreen,
    MainScreen,
    make_theme_screen,
)

# Methods live here but run on the composed PmemStat instance.


class KeyMixin:
    def do_key(self, key):
        """Route one key: modal widgets first, then the spinner's router."""
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
            self.needs_refresh = True
            return
        if key == ord('z'):
            if not self.opts.kill_mode:
                self.zap_confirm_start()
            self.needs_refresh = True
            return
        if key in (curses.KEY_ENTER, 10):
            if self.opts.kill_mode and self.stack.is_curr(MAIN_ST):
                self._start_kill_confirmation()
            return
        if key in self.spin.keys:
            self.spin.do_key(key, self.window)
            if key == ord('u'):
                self._set_units()
            elif key == ord('K') and self.stack.is_curr(MAIN_ST):
                self.screens[MAIN_ST].set_pick_mode(self.opts.kill_mode)
            self.needs_refresh = True

    def _start_kill_confirmation(self):
        """Ask 'y' to kill every process in the picked row."""
        win = self.window
        main_screen = self.screens[MAIN_ST]
        group = self.groups_by_line.get(main_screen.pick_pos, None)
        if not group:
            # Nothing picked: ENTER just leaves kill mode.
            self.opts.kill_mode = False
            main_screen.set_pick_mode(False)
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
            main_screen = self.screens[MAIN_ST]
            group = self.groups_by_line.get(main_screen.pick_pos, None)
            if group:
                pids = [x.pid for x in group.prcset]
                ok, message = KillThem(pids).do_kill()
                mark = Glyphs.get('check') if ok else Glyphs.get('cross')
                self.window.flash(f'{mark} {message}', duration=1.0)
            self.confirmation.cancel()
            self.window.passthrough_mode = False
            self.opts.kill_mode = False
            main_screen.set_pick_mode(False)
        elif result == 'cancelled':  # console-window's API spelling
            self.confirmation.cancel()
            self.window.passthrough_mode = False
        self.needs_refresh = True

    def window_loop(self):
        """Run the report under console-window's Screen model."""
        window_opts = ConsoleWindowOpts(
            head_line=True,
            return_if_pos_change=False,
            theme=getattr(self.opts, 'theme', None) or None)
        self.window = ConsoleWindow(opts=window_opts)
        self.screens = {MAIN_ST: MainScreen(self),
                        HELP_ST: HelpScreen(self),
                        THEMES_ST: make_theme_screen(self)}
        self.stack = ScreenStack(self.window, self.opts, SCREEN_NAMES,
                                 self.screens)
        self.spin = OptionSpinner(stack=self.stack,
                                  nav_keys=self.window.opts.nav_keys)
        spin = self.spin
        spin.add_key('help', '? - help screen', genre='action')
        spin.add_key('theme_screen', 't - theme picker', genre='action')
        spin.add_key('spin_theme', 't - cycle theme', genre='action')
        spin.add_key('kill_mode', 'K - kill mode', vals=[False, True],
                     obj=self.opts,
                     comments='Select line + ENTER to kill selected')
        spin.add_key('show_keys', 'K - show keys (demo mode)', genre='action')
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
        spin.add_key('growth_top', 'p - top-N growers', vals=['all', '10'],
                     obj=self.opts)
        spin.add_key('units', 'u - memory units', vals=['MB', 'KB', 'human'],
                     obj=self.opts)
        spin.add_key('cpu', 'c - show cpu', vals=[False, True], obj=self.opts)
        spin.add_key('psi', 'P - show PSI (header + memPSI%)',
                     vals=[False, True], obj=self.opts)
        handled = set(spin.keys) | {ord('/'), curses.KEY_ENTER, 10}
        if os.geteuid() == 0:
            handled.add(ord('z')) # zap offered only if running as root
        self.window.set_handled_keys(handled)
        was_groupby, was_others = self.opts.groupby, self.opts.others
        while self.running: # `q`/`^C` clear running
            if (self.opts.groupby != was_groupby
                    or self.opts.others != was_others):
                # A grouping change rebuilds every row ...
                was_groupby, was_others = self.opts.groupby, self.opts.others
                self.regroup_pending = True
                self.regroup_from_key = True
                self.needs_refresh = True
            current = self.stack.curr
            screen = self.screens[current.num]
            if current.num != MAIN_ST:
                screen.clear()
            screen.draw_screen()
            self.window.render()
            key = self.window.prompt(self.prompt_secs(self.opts.loop_secs))
            if key is None: # idle timeout...
                self.zap_confirm_expire()
                self.needs_refresh = True
            else:
                self.do_key(key)
            self.stack.perform_actions(self.spin)
        self.window.stop_curses()
