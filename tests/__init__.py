"""Test package for pmemstat.

Importing this package installs a lightweight stub for the ``console_window``
package *only when the real one cannot be imported*.  This lets the pure-logic
unit tests run in a minimal environment (for example a bare CI container) that
does not have the curses UI dependency installed.  When a real
``console_window`` is importable it always wins, so the stub never shadows the
genuine dependency.
"""
import sys
import types

#: True when the real ``console_window`` is importable, so tests can skip the
#: ones that need its Screen/ScreenStack machinery (the stub cannot provide it).
REAL_CONSOLE_WINDOW = True

try:  # Prefer the real dependency when it is available.
    import console_window  # noqa: F401
except ImportError:  # pragma: no cover - depends on the environment
    REAL_CONSOLE_WINDOW = False
    import curses

    _stub = types.ModuleType('console_window')
    for _name in ('ConsoleWindow', 'OptionSpinner',
                  'IncrementalSearchBar', 'InlineConfirmation'):
        setattr(_stub, _name, type(_name, (), {}))

    class _StubScreen:
        """Stand-in for Screen: enough for module import and subclassing."""

        def __init__(self, app=None):
            self.app = app
            self.win = getattr(app, 'window', None)

    class _StubScreenStack:
        """Stand-in for ScreenStack (tests that use it skip themselves)."""

        def __init__(self, win=None, obj=None, screens=(), screen_objects=None):
            self.win = win
            self.obj = obj
            self.stack, self.curr = [], None
            self.screen_objects = screen_objects or {}

    _stub.Screen = _StubScreen
    _stub.ScreenStack = _StubScreenStack

    # The semantic colour-pair ids, theme names and the picker factory must
    # match console-window exactly: main.py styles rows with Theme.<ROLE>,
    # builds its picker from Theme.create_picker_screen and the colour tests
    # assert the resulting attribute, so a drift here would make the stub
    # disagree with the real package instead of merely standing in for it.
    class _StubTheme:
        DEFAULT = 0
        DANGER = 1
        SUCCESS = 2
        WARNING = 3
        INFO = 4
        EMPHASIS = 5
        ERROR = 6
        PROGRESS = 7
        DEFAULT_BG = 8
        HOTSWAP = 9
        OLD_SUCCESS = 10

        THEMES = {name: {'name': name} for name in (
            'dark-mono', 'light-mono', 'default', 'solarized-dark',
            'solarized-light', 'gruvbox', 'nord')}

        _current = 'default'

        @classmethod
        def list_all(cls):
            return list(cls.THEMES)

        @classmethod
        def resolve(cls, theme_name):
            """Stand-in for Theme.resolve: an unknown name means 'default'."""
            return theme_name if theme_name in cls.THEMES else 'default'

        @classmethod
        def get_current(cls):
            """Stand-in for Theme.get_current."""
            return cls._current

        @classmethod
        def set_active(cls, theme_name):
            """Stand-in for Theme.set_active."""
            cls._current = theme_name
            return theme_name

        @classmethod
        def theme_of(cls, win):
            """Stand-in for Theme.theme_of."""
            return getattr(win, 'theme', cls._current)

        @classmethod
        def apply_to(cls, win, theme_name):
            """Stand-in for Theme.apply_to."""
            if theme_name not in cls.THEMES:
                return False
            setter = getattr(win, 'set_theme', None)
            if setter is not None:
                return bool(setter(theme_name))
            cls.set_active(theme_name)
            return True

        @classmethod
        def create_picker_screen(cls, base_class, *, cycle_key=None,
                                 store=None, note=None):
            """Minimal stand-in; the real UI tests are skipped with the stub."""

            class _Picker(base_class):
                """Stand-in picker; never exercised with the stub installed."""

                def draw_screen(self):
                    """No-op: the real-UI tests skip without console-window."""

                def theme_label(self, theme_name):
                    """Stand-in for the picker's header hook."""
                    return theme_name if theme_name else '(default)'

                def spin_theme_ACTION(self):  # pylint: disable=invalid-name
                    """No-op: the real-UI tests skip without console-window."""

            return _Picker

    _stub.Theme = _StubTheme

    class _StubConsoleWindowOpts:
        """Minimal stand-in that records the options an app passes."""

        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    _stub.ConsoleWindowOpts = _StubConsoleWindowOpts

    # Mirrors console_window.appearance: the role -> attribute fallback, the
    # process-wide switch and the one resolver main.py imports.
    _mono_roles = {
        _StubTheme.DANGER: curses.A_BOLD | curses.A_REVERSE,
        _StubTheme.ERROR: curses.A_BOLD | curses.A_REVERSE,
        _StubTheme.WARNING: curses.A_BOLD,
        _StubTheme.SUCCESS: curses.A_BOLD,
        _StubTheme.EMPHASIS: curses.A_BOLD,
        _StubTheme.INFO: curses.A_NORMAL,
        _StubTheme.PROGRESS: curses.A_NORMAL,
        _StubTheme.OLD_SUCCESS: curses.A_NORMAL,
        _StubTheme.HOTSWAP: curses.A_NORMAL,
    }
    _stub.MONO_ROLES = _mono_roles
    _stub_state = {'colors_enabled': True}

    def _set_colors_enabled(enabled):
        _stub_state['colors_enabled'] = bool(enabled)

    def _colors_enabled():
        return _stub_state['colors_enabled']

    def _semantic_attr(pair_id):
        if not _stub_state['colors_enabled']:
            return _mono_roles.get(pair_id, curses.A_NORMAL)
        try:
            if not curses.has_colors():
                return 0
            return curses.color_pair(pair_id)
        except curses.error:
            return 0

    def _resolve_appearance(cli_theme=None, no_color=False, saved_theme='',
                            saved_colors=True, environ=None):
        env = {} if environ is None else environ
        if no_color:
            colors = False
        elif cli_theme:
            colors = True
        elif env.get('NO_COLOR'):
            colors = False
        else:
            colors = bool(saved_colors)
        theme = cli_theme or saved_theme or ''
        if theme and theme not in _StubTheme.THEMES:
            theme = 'default'
        return theme, colors

    _stub.set_colors_enabled = _set_colors_enabled
    _stub.colors_enabled = _colors_enabled
    _stub.semantic_attr = _semantic_attr
    _stub.resolve_appearance = _resolve_appearance
    _stub.__version__ = '2.2.2'
    sys.modules['console_window'] = _stub
