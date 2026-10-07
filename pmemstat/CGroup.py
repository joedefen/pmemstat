#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Read authoritative Linux cgroup v2 accounting.

This module is intentionally dependency-free (standard library only) so that it
can be imported and unit-tested without the curses UI or the pinned
``console-window`` package.

Two independent capabilities are provided:

1. Parsing a process's membership from ``/proc/<pid>/cgroup``
   (:func:`parse_cgroup_lines` and :func:`cgroup_leaf`).
2. Reading the kernel's authoritative accounting for a cgroup directory
   (:class:`CGroup`): ``memory.current``, ``memory.stat`` and
   ``memory.pressure``.

The numbers from ``memory.current``/``memory.stat`` are *not* proportional
(PSS). They are the kernel's own per-cgroup accounting and are displayed
alongside pmemstat's proportional PSS numbers so the two can be compared.
"""
# CamelCase module name matches the package style (CpuSmooth, KillThem).
# pylint: disable=invalid-name

import os
import re

from pmemstat.Pressure import parse_pressure


def parse_cgroup_lines(lines):
    """Return the cgroup v2 (unified) path for a process.

    ``lines`` is the content of ``/proc/<pid>/cgroup`` split into lines, each
    of the form ``<hierarchy>:<controllers>:<path>``.

    The cgroup v2 unified hierarchy is identified by a ``0::`` prefix (no
    controllers). ``None`` is returned when no unified line is present, which
    is the case on a cgroup v1-only system.
    """
    for line in lines:
        parts = line.split(':', 2)
        if len(parts) != 3:
            continue
        hierarchy, controllers, path = parts
        if hierarchy.strip() == '0' and not controllers.strip():
            path = path.strip()
            return path or '/'
    return None


_XESCAPE_PAT = re.compile(r'\\x([0-9a-fA-F]{2})')


def decode_cgroup_name(name):
    """Decode the ``\\xNN`` escaping systemd applies to cgroup names.

    systemd escapes characters such as ``-`` as ``\\x2d`` inside cgroup paths
    (e.g. ``app-foo\\x2dbar.service``). Decoding affects only the *displayed*
    label; grouping always uses the raw, unambiguous path.
    """
    if not name:
        return name
    return _XESCAPE_PAT.sub(lambda match: chr(int(match.group(1), 16)), name)


def cgroup_leaf(path):
    """Return a short, human-friendly label for a cgroup path.

    The label is the final path component, which for systemd is the unit name
    (e.g. ``foo.service``). The unified root ``/`` is reported as ``(root)``
    and systemd's ``\\xNN`` escapes are decoded for readability.
    """
    if not path or path == '/':
        return '(root)'
    leaf = path.rstrip('/').rsplit('/', 1)[-1]
    return decode_cgroup_name(leaf) or '(root)'


def parse_memory_current(text):
    """Parse the single integer in ``memory.current`` (bytes) or return None."""
    try:
        return int(str(text).strip())
    except (TypeError, ValueError):
        return None


def parse_memory_stat(lines):
    """Parse ``memory.stat`` (``key value`` pairs) into a dict of ints."""
    stat = {}
    for line in lines:
        fields = line.split()
        if len(fields) != 2:
            continue
        try:
            stat[fields[0]] = int(fields[1])
        except ValueError:
            continue
    return stat


def parse_memory_pressure(lines):
    """Parse ``memory.pressure`` into ``{'some': {...}, 'full': {...}}``.

    ``avg*`` values are floats (percent) and ``total`` is an int
    (microseconds).

    All PSI files share one grammar, so this is a thin compatibility wrapper
    around :func:`pmemstat.Pressure.parse_pressure`.
    """
    return parse_pressure(lines)


def path_is_descendant(path, ancestor):
    """True when ``path`` lies strictly below ``ancestor`` in the cgroup tree."""
    base = ancestor if ancestor.endswith('/') else ancestor + '/'
    return path.startswith(base)


def has_descendant(keys, key):
    """True when any key in ``keys`` is a strict descendant of ``key``."""
    return any(other != key and path_is_descendant(other, key) for other in keys)


def local_values(raw_by_key, columns):
    """Convert hierarchical (raw) per-key values into per-key local values.

    ``raw_by_key`` maps a cgroup key to a dict of column values, as read from
    the kernel (each value already includes descendant cgroups). A key's
    *local* value for a column is its raw value minus the raw values of its
    descendant keys, i.e. that cgroup's own share. Returns
    ``{key: {column: local}}`` for the keys given.
    """
    keys = list(raw_by_key)
    result = {}
    for key in keys:
        raw = raw_by_key[key]
        local = {}
        for column in columns:
            value = raw.get(column, 0)
            for other in keys:
                if other != key and path_is_descendant(other, key):
                    value -= raw_by_key[other].get(column, 0)
            local[column] = value
        result[key] = local
    return result


class CGroup:
    """Authoritative cgroup v2 memory accounting for one cgroup path.

    ``path`` is the unified-hierarchy path as reported by
    :func:`parse_cgroup_lines` (e.g. ``/system.slice/foo.service``). The
    :attr:`root` attribute is the cgroup v2 mount point and is overridable,
    which makes the class trivial to unit-test against a temporary directory.
    """

    root = '/sys/fs/cgroup'

    def __init__(self, path):
        self.path = path if path else '/'

    @property
    def dir(self):
        """Absolute directory of this cgroup under :attr:`root`."""
        return os.path.join(self.root, self.path.lstrip('/'))

    def _read_text(self, name):
        """Return the text of a file in this cgroup dir, or ``None``."""
        try:
            with open(os.path.join(self.dir, name), encoding='utf-8') as fhandle:
                return fhandle.read()
        except OSError:
            # Missing file/dir (older kernels, cgroup v1) or no permission.
            return None

    def read_current(self):
        """``memory.current`` in bytes (or ``None`` when unavailable)."""
        text = self._read_text('memory.current')
        return None if text is None else parse_memory_current(text)

    def read_swap_current(self):
        """``memory.swap.current`` in bytes (or ``None`` when unavailable)."""
        text = self._read_text('memory.swap.current')
        return None if text is None else parse_memory_current(text)

    def read_stat(self):
        """``memory.stat`` as a dict of ints (empty dict when unavailable)."""
        text = self._read_text('memory.stat')
        return {} if text is None else parse_memory_stat(text.splitlines())

    def read_pressure(self):
        """``memory.pressure`` as ``{'some':..,'full':..}`` (empty if absent)."""
        text = self._read_text('memory.pressure')
        return {} if text is None else parse_memory_pressure(text.splitlines())

    def read(self):
        """Read all supported files at once.

        Returns a dict with keys ``current``, ``stat`` and ``pressure``. Any
        of them may be ``None``/empty when the underlying file is
        unavailable (older kernel, cgroup v1, or insufficient permission).
        """
        return {
            'current': self.read_current(),
            'swap_current': self.read_swap_current(),
            'stat': self.read_stat(),
            'pressure': self.read_pressure(),
        }
