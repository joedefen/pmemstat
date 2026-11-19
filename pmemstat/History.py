"""Boot-scoped, mutable growth ledger for pmemstat's cross-run leak tracking.

Storage layout (all inside one private directory):
-   <state_dir>/ledger.json   the ledger (the DB)
-   <state_dir>/ledger.lock   advisory lock guarding read-modify-write

The default directory is ``/tmp/pmemstat-<uid>``; when run as root,
ownership is given to actual user.

Writes are serialized with a non-blocking ``flock`` on ``ledger.lock``; if no lock, no write;
skips to avoid blocking (current lock holder writes something similar, just wee older).
The ledger itself is replaced atomically readers never see partial documents even w/o lock.

Treats a corrupt, unreadable or unknown-version ledger as empty.
"""

import fcntl
import json
import os
import socket
import tempfile
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version

SCHEMA_VERSION = 3  # if schema changes, old values just tossed

STATE_DIR_ENV = 'PMEMSTAT_STATE_DIR'   # to override location if need be
UPDATE_SECS_ENV = 'PMEMSTAT_HISTORY_UPDATE_SECS'
GRACE_SECS_ENV = 'PMEMSTAT_HISTORY_GRACE_SECS'

LEDGER_NAME = 'ledger.json'
LOCK_NAME = 'ledger.lock'

LEGACY_NAMES = ('history.jsonl', 'live.json', 'running') # v1 hist (purge if found)

DEFAULT_UPDATE_SECS = 300
GRACE_SECS = 600  # goner pids are retained for limited time

DIR_MODE = 0o700  # data privacy
FILE_MODE = 0o600

BOOT_ID_PATH = '/proc/sys/kernel/random/boot_id' # current boot UUID to id trash 

_NOFOLLOW = getattr(os, 'O_NOFOLLOW', 0) # refuse a planted symlink


# ---------------------------------------------------------------------------
# Ownership resolution
# ---------------------------------------------------------------------------
def _sudo_id(env_name, default):
    """``SUDO_<env_name>`` when running as root, else ``default``.

    ``default`` is evaluated eagerly by the caller (``os.geteuid()`` /
    ``os.getegid()``) but is only *used* when there is no usable override.
    """
    if os.geteuid() != 0:
        return default
    raw = os.environ.get(env_name)
    if raw:
        try:
            return int(raw)
        except ValueError:
            return default
    return default


def owning_uid():
    """The uid that should own the state: SUDO_UID when elevated, else euid."""
    return _sudo_id('SUDO_UID', os.geteuid())


def owning_gid():
    """The gid that should own the state: SUDO_GID when elevated, else egid."""
    return _sudo_id('SUDO_GID', os.getegid())


def _chown_to_owner(path):
    """Best-effort chown of ``path`` to the owning uid:gid.

    Only relevant after a root process created the file on behalf of the
    invoking user; failures are ignored (the data is still usable by root).
    """
    if os.geteuid() != 0:
        return
    uid = owning_uid()
    if uid == 0:
        return
    try:
        os.chown(path, uid, owning_gid())
    except OSError:
        pass


# ---------------------------------------------------------------------------
# State directory
# ---------------------------------------------------------------------------
def _default_state_root():
    """Per-uid default directory under the system temp dir."""
    return os.path.join(tempfile.gettempdir(), f'pmemstat-{owning_uid()}')


def state_dir(create=True):
    """Return the state directory, creating it (mode ``0700``) when asked."""
    override = os.environ.get(STATE_DIR_ENV)
    path = override if override else _default_state_root()
    if create:
        os.makedirs(path, mode=DIR_MODE, exist_ok=True)
        _chown_to_owner(path)
    return path


def _state_path(name, create=False):
    """Path to ``name`` inside the state directory."""
    return os.path.join(state_dir(create=create), name)


# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------
def current_boot():
    """Contents of the kernel boot UUID, or ``''`` when unavailable."""
    try:
        with open(BOOT_ID_PATH, encoding='ascii') as handle:
            return handle.read().strip()
    except OSError:
        return ''


def _hostname():
    """Best-effort hostname (never raises)."""
    try:
        return socket.gethostname()
    except OSError:
        return 'unknown'


def tool_version():
    """Installed pmemstat version, or ``'unknown'`` when not resolvable."""
    try:
        return version('pmemstat')
    except PackageNotFoundError:
        return 'unknown'
    except Exception:
        return 'unknown'


def _iso(stamp):
    """ISO-8601 rendering of an aware datetime."""
    return stamp.isoformat()


def _parse_ts(value):
    """Parse an ISO-8601 timestamp into an aware datetime, or ``None``."""
    if not isinstance(value, str):
        return None
    try:
        stamp = datetime.fromisoformat(value)
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp


# ---------------------------------------------------------------------------
# Low-level file helpers
# ---------------------------------------------------------------------------
def _atomic_write(path, data):
    """Write ``data`` (bytes) to ``path`` via a temp file + rename + fsync."""
    directory = os.path.dirname(path) or '.'
    fd, tmp = tempfile.mkstemp(dir=directory, prefix='.tmp-')
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)
    _chown_to_owner(path)


# ---------------------------------------------------------------------------
# Ledger (ledger.json)
# ---------------------------------------------------------------------------
def _empty_ledger(boot, now=None):
    """Build an empty, schema-valid ledger for ``boot``."""
    stamp = now if now is not None else datetime.now(timezone.utc)
    return {
        'v': SCHEMA_VERSION,
        'boot_id': current_boot() if boot is None else boot,
        'host': _hostname(),
        'uid': owning_uid(),
        'tool': tool_version(),
        'updated_ts': _iso(stamp),
        'views': {},
        'sys': {},
    }


def load_ledger():
    """Return the ledger as a dict, or ``None`` when missing/corrupt/outdated."""
    try:
        fd = os.open(_state_path(LEDGER_NAME, create=False),
                     os.O_RDONLY | _NOFOLLOW)
    except OSError:
        return None
    try:
        with os.fdopen(fd, 'r', encoding='utf-8', errors='replace') as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get('v') != SCHEMA_VERSION:
        return None
    if not isinstance(data.get('views'), dict):
        return None
    return data


def baselines(ledger=None, boot=None):
    """First-seen baselines for the current boot, as a seeding map.

    - Returns ``{view: {key: {'base': kb, 'base_ts': iso}}}``.
    """
    if ledger is None:
        ledger = load_ledger()
    if not isinstance(ledger, dict) or ledger.get('v') != SCHEMA_VERSION:
        return {}
    wanted = current_boot() if boot is None else boot
    if ledger.get('boot_id') != wanted:
        return {}
    result = {}
    for view, by_key in (ledger.get('views') or {}).items():
        if not isinstance(by_key, dict):
            continue
        entries = {}
        for key, entry in by_key.items():
            if not isinstance(entry, dict):
                continue
            try:
                base = int(entry['base'])
            except (KeyError, TypeError, ValueError):
                continue
            entries[str(key)] = {'base': base, 'base_ts': entry.get('base_ts')}
        if entries:
            result[str(view)] = entries
    return result


def sys_baselines(ledger=None, boot=None):
    """First-seen system-metric baselines for the current boot.

    - Returns ``{key: {'base': kb, 'base_ts': iso}}`` for the system metrics
      (``Used``/``TOTALS``/``ShTmp``/``OthK``/``OthU``)
    - header deltas share same baseline/interval as per-process growth
    """
    if ledger is None:
        ledger = load_ledger()
    if not isinstance(ledger, dict) or ledger.get('v') != SCHEMA_VERSION:
        return {}
    wanted = current_boot() if boot is None else boot
    if ledger.get('boot_id') != wanted:
        return {}
    result = {}
    for key, entry in (ledger.get('sys') or {}).items():
        if not isinstance(entry, dict):
            continue
        try:
            base = int(entry['base'])
        except (KeyError, TypeError, ValueError):
            continue
        result[str(key)] = {'base': base, 'base_ts': entry.get('base_ts')}
    return result


def _merge_flat(dest, by_key, now_iso):
    """Add/refresh flat ``{key: value}`` entries (``base`` fixed, ``last`` fresh)."""
    for key, value in (by_key or {}).items():
        try:
            value = int(value)
        except (TypeError, ValueError):
            continue
        key = str(key)
        entry = dest.get(key)
        if not isinstance(entry, dict):
            dest[key] = {'base': value, 'base_ts': now_iso,
                         'last': value, 'last_ts': now_iso}
        else:
            entry['last'] = value
            entry['last_ts'] = now_iso


def _gc_flat(dest, now, grace):
    """Drop entries absent (by ``last_ts``) for longer than ``grace``; return kept."""
    for key in list(dest):
        entry = dest[key]
        stamp = _parse_ts(entry.get('last_ts') if isinstance(entry, dict)
                          else None)
        if stamp is None or (now - stamp).total_seconds() > grace:
            del dest[key]
    return bool(dest)


def _merge_ledger(ledger, samples, sys_samples, now, boot, grace):
    """Merge ``samples`` and ``sys_samples`` into ``ledger`` (add/update + GC)."""
    if (not isinstance(ledger, dict)
            or ledger.get('v') != SCHEMA_VERSION
            or ledger.get('boot_id') != boot):
        ledger = _empty_ledger(boot, now=now)
    views = ledger['views']
    sysdest = ledger.get('sys')
    if not isinstance(sysdest, dict):
        sysdest = {}
    ledger['sys'] = sysdest
    now_iso = _iso(now)
    for view, by_key in (samples or {}).items():
        dest = views.setdefault(str(view), {})
        if not isinstance(dest, dict):
            dest = {}
            views[str(view)] = dest
        _merge_flat(dest, by_key, now_iso)
    _merge_flat(sysdest, sys_samples, now_iso)
    for view in list(views):
        dest = views[view]
        if not isinstance(dest, dict) or not _gc_flat(dest, now, grace):
            del views[view]
    _gc_flat(sysdest, now, grace)
    ledger.update({'v': SCHEMA_VERSION, 'boot_id': boot, 'host': _hostname(),
                   'uid': owning_uid(), 'tool': tool_version(),
                   'updated_ts': now_iso})
    return ledger


def _normalize_meta(boot, now):
    """Resolve ``boot``/``now`` defaults for a ledger write (aware UTC ``now``)."""
    boot = current_boot() if boot is None else boot
    now = datetime.now(timezone.utc) if now is None else now
    if isinstance(now, datetime) and now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return boot, now


def _write_ledger(samples, sys_samples, boot, now, fresh):
    """Locked read-modify-write of the ledger; returns ``True`` when written.

    - ``fresh`` starts from an empty ledger; else, preserves existing baselines
    - Returns ``False`` when the write is skipped (another pmemstat holds the lock,
      or the state directory is unusable).
    - Never blocks or raises for a busy lock.
    """
    try:
        lock_path = _state_path(LOCK_NAME, create=True)
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | _NOFOLLOW, FILE_MODE)
    except OSError:
        return False
    try:
        _chown_to_owner(lock_path)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False
        base = _empty_ledger(boot, now=now) if fresh else load_ledger()
        ledger = _merge_ledger(base, samples, sys_samples, now, boot,
                               gc_grace_secs())
        try:
            body = json.dumps(ledger, separators=(',', ':')).encode('utf-8')
            _atomic_write(_state_path(LEDGER_NAME, create=True), body)
        except OSError:
            return False
        return True
    finally:
        os.close(fd)


def update_ledger(samples, sys_samples=None, boot=None, now=None):
    """Merge ``samples`` into the ledger under an exclusive non-blocking lock."""
    boot, now = _normalize_meta(boot, now)
    return _write_ledger(samples, sys_samples, boot, now, fresh=False)


def reset_ledger(samples, sys_samples=None, boot=None, now=None):
    """Replace the ledger with ``samples`` as the new baseline measured from now."""
    boot, now = _normalize_meta(boot, now)
    return _write_ledger(samples, sys_samples, boot, now, fresh=True)


def cleanup_obsolete():
    """Best-effort removal of files from the obsolete phase-1 anchor design."""
    for name in LEGACY_NAMES:
        try:
            os.remove(_state_path(name, create=False))
        except OSError:
            pass


##### Periodic-write configuration ...
def _float_env(name, default):
    """Positive float from ``name`` in the environment, else ``default``."""
    raw = os.environ.get(name)
    if raw:
        try:
            value = float(raw)
            if value > 0:
                return value
        except ValueError:
            pass
    return default


def update_secs():
    """Periodic ledger-write interval in seconds, overridable via the env."""
    return _float_env(UPDATE_SECS_ENV, DEFAULT_UPDATE_SECS)


def gc_grace_secs():
    """Absent-key grace period in seconds, overridable via the env."""
    return _float_env(GRACE_SECS_ENV, GRACE_SECS)
