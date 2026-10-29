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

    # The semantic colour-pair ids must match console-window exactly: main.py
    # styles rows with Theme.<ROLE> and the colour tests assert the resulting
    # attribute, so a drift here would make the stub disagree with the real
    # package instead of merely standing in for it.
    _stub.Theme = type('Theme', (), {
        'DEFAULT': 0, 'DANGER': 1, 'SUCCESS': 2, 'WARNING': 3, 'INFO': 4,
        'EMPHASIS': 5, 'ERROR': 6, 'PROGRESS': 7, 'DEFAULT_BG': 8,
        'HOTSWAP': 9, 'OLD_SUCCESS': 10,
    })

    class _StubConsoleWindowOpts:
        """Minimal stand-in that records the options an app passes."""

        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    _stub.ConsoleWindowOpts = _StubConsoleWindowOpts
    # Must match the exact console-window pin in pyproject.toml (main.py
    # refuses to start when the reported version differs from that pin).
    _stub.__version__ = '2.0.0.9'
    sys.modules['console_window'] = _stub
