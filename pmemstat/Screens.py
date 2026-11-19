"""console-window screens for pmemstat."""
import curses
import os
from datetime import datetime

from console_window import BasicHelpScreen, Screen, Theme, semantic_attr

from pmemstat.UiPrefs import ThemePrefs

# Screen identities for the ScreenStack; MAIN is the report itself.
MAIN_ST, HELP_ST, THEMES_ST = 0, 1, 2
SCREEN_NAMES = ('MAIN', 'HELP', 'THEMES')

# ###################################################################################
# ##### Screens (console-window ScreenStack model)
# ###################################################################################
class PmemScreen(Screen):
    """Base screen; the report owner (``PmemStat``) is ``self.app``.

    Overrides the library's ``quit_ACTION()`` for controlled exit.
    """

    app: 'PmemStat'

    def quit_ACTION(self):
        """Stop the report loop; ``main()`` owns the shutdown."""
        self.app.running = False


class MainScreen(PmemScreen):
    """The memory report itself."""

    def draw_screen(self):
        app = self.app
        if not app.needs_refresh:
            return
        # clear() both resets this screen's content and makes it the window's
        # active view; the emit helpers ask the stack for the current screen.
        self.clear()
        app.loop(datetime.now(), is_first=app.first_loop,
                 regroup=app.regroup_pending)
        app.first_loop = False
        app.regroup_pending = False
        app.needs_refresh = False

    def on_resume(self):
        # Whatever happened on the other screen (a theme change,
        # a grouping change made before it) the report should be redrawn from new data.
        self.app.needs_refresh = True
        return True

    def help_ACTION(self):
        """'?' opens the Help screen from the report."""
        self.app.stack.push(HELP_ST, self.pick_pos)

    def theme_screen_ACTION(self):
        """'t' opens the theme picker from the report."""
        self.app.stack.push(THEMES_ST, self.pick_pos)


class HelpScreen(PmemScreen, BasicHelpScreen):
    """The built-in help page plus this app's own notes and live prompt."""

    def extra_help_paragraphs(self):
        """The prose under ``---------- Instructions ----------``."""
        if os.geteuid() != 0:
            return ['Hint: run "sudo pmemstat" to show all PIDs']
        return ['z - zap the growth history (current values become baseline) '
                '- asks for "y"/ESC']

    def draw_screen(self):
        """Let the library draw the page, then add the live zap prompt."""
        super().draw_screen()
        if self.app.zap_confirm_active():
            # An armed 'z' also asks for its answer here,
            # so the prompt is never invisible while it counts down.
            self.add_body(self.app.zap_confirm_text(),
                          attr=curses.A_BOLD | semantic_attr(Theme.DANGER))

    def help_ACTION(self):
        """'?' closes Help again (ESC does the same via escape_ACTION)."""
        self.app.stack.pop()

    def theme_screen_ACTION(self):
        """'t' opens the theme picker; ESC there returns to Help."""
        self.app.stack.push(THEMES_ST, self.pick_pos)

    def show_keys_ACTION(self):
        """'K' toggles demo mode: the last key pressed is shown on screen."""
        win = self.app.window
        win.set_demo_mode(enabled=None)          # None toggles
        state = 'on' if getattr(win.opts, 'demo_mode', False) else 'off'
        win.status(f'demo mode: {state}')
        self.app.needs_refresh = True


def make_theme_screen(app):
    """console-window's stock picker, wired to pmemstat's preference store."""
    prefs = getattr(app, 'prefs', None)
    picker = Theme.create_picker_screen(
        PmemScreen, cycle_key='t',
        store=ThemePrefs(prefs) if prefs is not None else None)
    return picker(app)
