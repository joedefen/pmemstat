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

try:  # Prefer the real dependency when it is available.
    import console_window  # noqa: F401
except ImportError:  # pragma: no cover - depends on the environment
    _stub = types.ModuleType('console_window')
    for _name in ('ConsoleWindow', 'OptionSpinner',
                  'IncrementalSearchBar', 'InlineConfirmation'):
        setattr(_stub, _name, type(_name, (), {}))
    _stub.__version__ = '1.4.3'
    sys.modules['console_window'] = _stub
