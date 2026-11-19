"""Debug tracing support for pmemstat."""
import sys
from io import StringIO

DebugLevel = 0


def DB(level, *opts, **kwargs):
    """Debug message printer.  - printing is conditional on DebugLevel being no smaller
    than the passed level - level 0 is unconditional.  It is use for temporary traces that
      are commented out when the debug need is gone.
    """
    # level={level} do_debug={bool(DebugLevel>=level)}')
    if DebugLevel >= level:
        lineno = sys._getframe(1).f_lineno
        tstr = StringIO()
        print(f'DB{level}', end=' ', file=tstr)
        kwargs['end'] = ' '
        kwargs['file'] = tstr
        print(*opts, **kwargs)
        print(tstr.getvalue() + f'[:{lineno}]')
