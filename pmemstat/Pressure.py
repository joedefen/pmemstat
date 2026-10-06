#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Read and format Linux Pressure Stall Information (PSI).

The kernel exposes PSI through ``/proc/pressure/{memory,cpu,io}`` (see
``Documentation/accounting/psi.rst``). Each file contains one or two lines::

    some avg10=0.42 avg60=0.10 avg300=0.02 total=123456
    full avg10=0.00 avg60=0.00 avg300=0.00 total=0

``memory.pressure`` and ``io.pressure`` have both the ``some`` and ``full``
lines, whereas ``cpu.pressure`` only ever has ``some`` (there is no such thing
as "all CPUs stalled"). ``avg*`` values are stall-time percentages over a
10/60/300 second window and ``total`` is cumulative microseconds.

This module is intentionally dependency-free (standard library only) so that it
can be imported and unit-tested without the curses UI or the pinned
``console-window`` package. The parse grammar is shared with
:func:`pmemstat.CGroup.parse_memory_pressure`, which delegates here.
"""
# CamelCase module name matches the package style (CGroup, CpuSmooth, KillThem).
# pylint: disable=invalid-name

import os

# Display order and short labels: memory first because pmemstat is a memory tool.
# A '%' is appended to each label in format_pressure_lines(), so these spell out
# the full metric name (e.g. 'memPSI%') to mirror the report's column labels.
RESOURCES = ('memory', 'cpu', 'io')
_SHORT = {'memory': 'memPSI', 'cpu': 'cpuPSI', 'io': 'ioPSI'}
AVG_KEYS = ('avg10', 'avg60', 'avg300')
_STALLS = ('some', 'full')


def parse_pressure(lines):
    """Parse a PSI file into ``{'some': {...}, 'full': {...}}``.

    ``avg*`` values are floats (percent) and ``total`` is an int
    (microseconds). Malformed lines/tokens are ignored.
    """
    pressure = {}
    for line in lines:
        fields = str(line).split()
        if len(fields) < 2 or fields[0] not in _STALLS:
            continue
        entry = {}
        for token in fields[1:]:
            key, sep, value = token.partition('=')
            if not sep:
                continue
            try:
                entry[key] = (float(value) if key in AVG_KEYS
                              else int(value))
            except ValueError:
                continue
        pressure[fields[0]] = entry
    return pressure


def read_system_pressure(root='/proc/pressure'):
    """Read every available PSI file under ``root``.

    Returns ``{resource: {'some': {...}, 'full': {...}}}``; resources whose
    file is missing or unreadable (for example when the kernel was built with
    ``CONFIG_PSI=n``) are omitted.
    """
    snapshot = {}
    for resource in RESOURCES:
        try:
            with open(os.path.join(root, resource), encoding='utf-8') as fhandle:
                lines = fhandle.read().splitlines()
        except OSError:
            # Missing file (no PSI support) or no permission.
            continue
        parsed = parse_pressure(lines)
        if parsed:
            snapshot[resource] = parsed
    return snapshot


_WINDOW_LABELS = ('10s', '60s', '300s')
_CELL_WIDTH = 6
_CELL_GAP = 2                            # spaces between value cells
_GROUP_WIDTH = _CELL_WIDTH * 3 + _CELL_GAP * 2   # some/all window group
_LABEL_WIDTH = 7                         # e.g. 'memPSI%'
_LABEL_GAP = 2                           # row label -> first group
_GROUP_GAP = 7                           # some group -> full group (separates)
_TITLE_LABEL_SHIFT = 2                   # leaves one space before the 1st window
_SOME_COL = _LABEL_WIDTH + _LABEL_GAP
_FULL_COL = _SOME_COL + _GROUP_WIDTH + _GROUP_GAP
_TOTAL_WIDTH = _FULL_COL + _GROUP_WIDTH


def _cell(entry, key):
    """One fixed-width value cell; ``-`` when the kernel omitted the field."""
    if not entry or key not in entry:
        return '-'.rjust(_CELL_WIDTH)
    return f'{entry[key]:{_CELL_WIDTH}.2f}'


def _row(entry):
    """The three window cells for one stall entry, space separated."""
    return (' ' * _CELL_GAP).join(_cell(entry, key) for key in AVG_KEYS)


def _windows_title():
    """``10s``/``60s``/``300s`` right-aligned over the value cells."""
    return (' ' * _CELL_GAP).join(
        f'{label:>{_CELL_WIDTH}}' for label in _WINDOW_LABELS)


def _title_line():
    """The heading row: ``SOME``/``FULL`` over their columns.

    Each stall label is shifted right so exactly one space separates it from
    the right-aligned window labels beneath (the label overwrites part of the
    first cell's leading padding, so the windows are drawn first).
    """
    buf = [' '] * _TOTAL_WIDTH

    def put(text, col):
        for offset, char in enumerate(text):
            buf[col + offset] = char

    for label, col in (('SOME', _SOME_COL), ('FULL', _FULL_COL)):
        put(_windows_title(), col)
        put(label, col - len(label) + _TITLE_LABEL_SHIFT)
    return ''.join(buf)


def format_pressure_lines(snapshot, resources=RESOURCES):
    """Render a snapshot as a fixed-width PSI table ([] when empty).

    The block is four lines: a heading, then one row per resource, each with a
    ``SOME`` and a ``FULL`` group of ``avg10``/``avg60``/``avg300`` cells::

            SOME 10s     60s    300s     FULL 10s     60s    300s
         memPSI%    1.23    0.45    0.20         0.10    0.05    0.02
         cpuPSI%    0.00    0.03    0.00            -       -       -
          ioPSI%    0.15    0.60    1.04         0.10    0.49    0.96

    The heading line is intended to be drawn bold. Resources whose file is
    absent are skipped. When a resource provides no ``full`` line (as
    ``cpu.pressure`` does on many kernels) its ``full`` cells are shown as
    ``-``. An empty snapshot yields an empty list.
    """
    present = [resource for resource in resources if snapshot.get(resource)]
    if not present:
        return []
    lines = [_title_line()]
    sep = ' ' * _GROUP_GAP
    for resource in present:
        entry = snapshot[resource]
        label = f'{_SHORT.get(resource, resource)}%'
        lines.append(f'{label:>{_LABEL_WIDTH}}' + ' ' * _LABEL_GAP
                     + _row(entry.get('some')) + sep
                     + _row(entry.get('full')))
    return lines
