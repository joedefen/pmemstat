"""Growth baseline tracking and ledger seeding."""
from datetime import datetime, timezone


class GrowthTracker:
    """Geometric multi-resolution growth baseline.

    - Anchors are at ``16 * 4**k`` seconds.
    - The oldest anchor still within ``RETENTION`` times its own age is the base.
    - Growth is measured against the base's high-water mark
      and clamped at zero; anchor values never decrease.
    - The first 16 seconds are not counted to ignore startup "growth".
    - Each anchor rebase may step the growth down ~10% (discounting old growth)
    """
    BASE = 16
    RETENTION = 32

    def __init__(self):
        self.t0 = None
        self.peak = None
        self.snaps = []          # list of (anchor_age, high_water_value)
        self._next_age = self.BASE
        self.last_seen = None    # monotonic time of the last fed sample

    def reset(self):
        """Forget all growth history (used by zap_history)."""
        self.t0 = None
        self.peak = None
        self.snaps = []
        self._next_age = self.BASE
        # NOTE: self.last_seen is preserved; it is bookkeeping aid, not history.

    def seed(self, now, base_val, peak, interval):
        """Pre-load a baseline carried over from a previous run's ledger."""
        interval = max(1, interval)
        self.t0 = now - interval
        self.peak = peak
        self.snaps = [(0, base_val)]
        self._next_age = max(interval, self.BASE) * 4
        self.last_seen = now

    def update(self, now, value):
        """Feed a sample; return ``(growth, interval)`` in (KB, secs) or None."""
        if value is None:
            return None
        if self.t0 is None:
            self.t0 = now
            self.peak = value
            return None
        age = now - self.t0
        while age >= self._next_age:
            self.snaps.append((self._next_age,
                               self.peak if self.peak is not None else value))
            self._next_age *= 4
        if self.peak is None or value > self.peak:
            self.peak = value
        while (len(self.snaps) > 1 and self.snaps[0][0] > 0
               and age >= self.RETENTION * self.snaps[0][0]):
            self.snaps.pop(0)
        if not self.snaps or age <= self.snaps[0][0]:
            return None
        base_age, base_val = self.snaps[0]
        return max(value - base_val, 0), age - base_age


def _parse_iso(value):
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


def _as_datetime(value):
    """Coerce epoch seconds to an aware UTC ``datetime``; pass datetimes through."""
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, timezone.utc)
    return value


def _baseline_parts(entry, now_wall):
    """Return ``(base_kb, interval_secs)`` for a ledger baseline entry, else None."""
    if not isinstance(entry, dict):
        return None
    try:
        base = int(entry['base'])
    except (KeyError, TypeError, ValueError):
        return None
    stamp = _parse_iso(entry.get('base_ts'))
    interval = ((now_wall - stamp).total_seconds()
                if stamp is not None else 1.0)
    return base, max(1.0, interval)


def seed_from_baselines(trackers, baselines, samples, now_mono, now_wall):
    """Seed per-``(view, key)`` trackers from ledger baselines and first samples."""
    seeded = set()
    if not baselines:
        return seeded
    now_wall = _as_datetime(now_wall)
    for view, by_key in (samples or {}).items():
        base_view = baselines.get(view) or {}
        for key, current in by_key.items():
            parts = _baseline_parts(base_view.get(key), now_wall)
            if parts is None:
                continue
            base, interval = parts
            skey = (view, key)
            tracker = trackers.get(skey)
            if tracker is None:
                tracker = GrowthTracker()
                trackers[skey] = tracker
            tracker.seed(now_mono, min(base, current), max(base, current),
                         interval)
            seeded.add(skey)
    return seeded


def seed_sys_from_baselines(trackers, baselines, samples, now_mono, now_wall):
    """Seed the flat system trackers from ledger system baselines."""
    seeded = set()
    if not baselines:
        return seeded
    now_wall = _as_datetime(now_wall)
    for key, current in (samples or {}).items():
        parts = _baseline_parts(baselines.get(key), now_wall)
        if parts is None:
            continue
        base, interval = parts
        tracker = trackers.get(key)
        if tracker is None:
            tracker = GrowthTracker()
            trackers[key] = tracker
        tracker.seed(now_mono, min(base, current), max(base, current), interval)
        seeded.add(key)
    return seeded
