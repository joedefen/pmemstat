#!/usr/bin/env python3
"""Durable UI preferences for pmemstat (the color theme).

These are user preferences that must survive reboots,
so they live in config dir of the *invoking* user.

``ThemePrefs`` adapts this store to console-window's
two-method ``ThemeStore`` protocol (``load() -> str | None``, ``save(name)``),
so the library itself never opens a file.
"""

import json
import os
import pwd
import tempfile

CONFIG_DIR_ENV = 'PMEMSTAT_CONFIG_DIR'
APP_DIR = 'pmemstat'
FILE_NAME = 'ui.json'

# The type is enforced on load
DEFAULTS = {
    # '' means "whatever console-window calls the default";
    # 'mono' is the library's real no-color theme.
    'theme': '',
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
    """Directory holding ``ui.json``; created (``0700``) unless told otherwise."""
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
    """A JSON-backed ``{name: value}`` preference store (see the module doc)."""

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
        # A remembered "color off" becomes the mono theme.
        if loaded.get('colors') is False:
            loaded['theme'] = 'mono'
        for key, default in DEFAULTS.items():
            value = loaded.get(key)
            # Compare types exactly: bool is an int subclass,
            # so a corrupt "theme": 1 would otherwise pass an isinstance() test.
            if type(value) is type(default):
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
        if type(value) is not expected:
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
    """Adapter: console-window's ``ThemeStore`` over :class:`UiPrefs`."""

    def __init__(self, prefs):
        self.prefs = prefs

    def load(self):
        """The remembered theme name, or ``None`` ("nothing remembered")."""
        return self.prefs.get('theme') or None

    def save(self, name):
        """Remember *name* (``''``/``None`` forgets it)."""
        self.prefs.set('theme', name or '')
        self.prefs.save()
