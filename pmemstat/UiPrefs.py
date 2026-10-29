#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Durable UI preferences for pmemstat (the colour theme and whether colour is on).

This is deliberately *not* :mod:`pmemstat.History`.  The history ledger is
per-boot bookkeeping that belongs in a temp directory; these are user
preferences that must survive reboots, so they live in the XDG config dir of the
*invoking* user.  When pmemstat runs as root via ``sudo`` (the recommended way),
``SUDO_USER`` identifies the real user, so a root run writes preferences the
user can actually see instead of burying them under ``/root``.

The store is tolerant by design.  A missing, unreadable or corrupt file yields
the defaults rather than stopping the tool, unknown keys are dropped on the next
save, and a failed save is reported by return value -- never by an exception,
because losing a remembered theme must not take the program down.

``ThemePrefs`` adapts this store to console-window's two-method ``ThemeStore``
protocol (``load() -> str | None``, ``save(name)``), which is the seam the
library's theme picker uses so the library itself never opens a file.
"""
# CamelCase module name matches the package style (CGroup, CpuSmooth, KillThem).
# pylint: disable=invalid-name

import json
import os
import pwd
import tempfile

#: Override the config directory (tests, and users who keep dotfiles elsewhere).
CONFIG_DIR_ENV = 'PMEMSTAT_CONFIG_DIR'
#: Subdirectory of the config root.
APP_DIR = 'pmemstat'
#: Preference file name inside the app directory.
FILE_NAME = 'ui.json'

#: Every preference and its type.  The type is enforced on load and on set:
#: a hand-edited ``"colors": 1`` must be ignored, not treated as truthy, and an
#: unknown key must never appear in the saved file.
DEFAULTS = {
    # '' means "whatever console-window calls the default"; 'mono' is
    # pmemstat's own no-colour pseudo-theme (see main.MONO_THEME).
    'theme': '',
    'colors': True,
}


def _real_user_ids():
    """``(uid, gid)`` of the invoking user under sudo, else ``None``.

    ``None`` means "we are not root-by-proxy", so nothing needs re-owning.
    """
    name = os.environ.get('SUDO_USER')
    if not name:
        return None
    try:
        record = pwd.getpwnam(name)
    except KeyError:
        return None
    return record.pw_uid, record.pw_gid


def _chown_to_real_user(path):
    """Best-effort hand-back of a path we created while running under sudo."""
    ids = _real_user_ids()
    if ids is None:
        return
    try:
        os.chown(path, ids[0], ids[1])
    except OSError:
        pass  # not root, or a filesystem that refuses: never fatal


def config_dir(create=True):
    """Directory holding ``ui.json``; created (``0700``) unless told otherwise.

    ``PMEMSTAT_CONFIG_DIR`` wins outright (it is also how the tests avoid
    touching a real home directory).  Otherwise the home of ``SUDO_USER`` (when
    set) or the current user is used under ``$XDG_CONFIG_HOME`` or ``~/.config``.
    """
    override = os.environ.get(CONFIG_DIR_ENV)
    if override:
        path = override
    else:
        name = os.environ.get('SUDO_USER')
        if name:
            try:
                home = pwd.getpwnam(name).pw_dir
            except KeyError:
                home = os.path.expanduser('~')
            root = os.path.join(home, '.config')
        else:
            root = (os.environ.get('XDG_CONFIG_HOME')
                    or os.path.join(os.path.expanduser('~'), '.config'))
        path = os.path.join(root, APP_DIR)
    if create:
        os.makedirs(path, mode=0o700, exist_ok=True)
        _chown_to_real_user(path)
    return path


class UiPrefs:
    """A JSON-backed ``{name: value}`` preference store (see the module doc).

    ``path`` defaults to ``<config_dir>/ui.json``; tests pass their own.  The
    file is written atomically (temp file + ``os.replace``) so an interrupted
    save can never leave a half-written preference file behind.
    """

    def __init__(self, path=None):
        self.path = path if path is not None else os.path.join(
            config_dir(create=False), FILE_NAME)
        self.values = dict(DEFAULTS)
        self.dirty = False
        self.load()

    def load(self):
        """Merge the file into the defaults; silently keep defaults on failure."""
        try:
            with open(self.path, encoding='utf-8') as handle:
                loaded = json.load(handle)
        except (OSError, ValueError):
            # Missing file, no permission, or not JSON: defaults stand.
            return
        if not isinstance(loaded, dict):
            return
        for key, default in DEFAULTS.items():
            value = loaded.get(key)
            # Compare types exactly: bool is an int subclass, so a corrupt
            # "colors": 1 would otherwise pass an isinstance() test.
            if type(value) is type(default):  # pylint: disable=unidiomatic-typecheck
                self.values[key] = value
        if set(loaded) - set(DEFAULTS):
            # Obsolete keys are cleaned up on the next save.
            self.dirty = True

    def get(self, key):
        """Current value of *key* (the default when somehow absent)."""
        return self.values.get(key, DEFAULTS.get(key))

    def set(self, key, value):
        """Set *key*, marking the store dirty when the value actually changes."""
        if key not in DEFAULTS:
            raise KeyError(f'unknown preference {key!r}')
        expected = type(DEFAULTS[key])
        if type(value) is not expected:  # pylint: disable=unidiomatic-typecheck
            raise TypeError(
                f'preference {key!r} must be {expected.__name__}, '
                f'got {type(value).__name__}')
        if self.values.get(key) != value:
            self.values[key] = value
            self.dirty = True

    def save(self):
        """Write the store atomically; return ``False`` if it could not be saved."""
        if not self.dirty:
            return True
        directory = os.path.dirname(self.path) or '.'
        temp_path = None
        try:
            os.makedirs(directory, mode=0o700, exist_ok=True)
            _chown_to_real_user(directory)
            handle, temp_path = tempfile.mkstemp(
                dir=directory, prefix='.ui-', suffix='.json')
            with os.fdopen(handle, 'w', encoding='utf-8') as stream:
                json.dump(self.values, stream, indent=2)
                stream.write('\n')
            os.chmod(temp_path, 0o600)
            _chown_to_real_user(temp_path)
            os.replace(temp_path, self.path)
        except OSError:
            if temp_path is not None:
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass
            return False
        self.dirty = False
        return True


class ThemePrefs:
    """Adapter: console-window's ``ThemeStore`` over :class:`UiPrefs`.

    The library never opens a file; it only knows these two methods.  Passing
    this object to a theme picker therefore makes the choice stick with no
    knowledge of paths or JSON in the UI layer.
    """

    def __init__(self, prefs):
        self.prefs = prefs

    def load(self):
        """The remembered theme name, or ``None`` ("nothing remembered")."""
        return self.prefs.get('theme') or None

    def save(self, name):
        """Remember *name* (``''``/``None`` forgets it)."""
        self.prefs.set('theme', name or '')
        self.prefs.save()
