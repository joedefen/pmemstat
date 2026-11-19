"""Shared constants and cgroup helpers for the PmemStat mixins."""

CGROUP_GROUPS = ('cgroup', 'cgroupCharge')
GROWTH_VIEWS = ('exe', 'cmd', 'pid') + CGROUP_GROUPS
HISTORY_ENV_DISABLE = 'PMEMSTAT_NO_HISTORY'


def is_cgroup_groupby(groupby):
    """True when ``groupby`` selects a cgroup v2 grouping (either view)."""
    return groupby in CGROUP_GROUPS


def cgroup_view_totals(data):
    """Raw (hierarchical) ``footprt``/``kcharge`` totals in KB from a read."""
    stat = data['stat'] or {}
    if data['current'] is None:
        return None
    current = data['current']
    swap = stat.get('swap')
    if swap is None:
        swap = data.get('swap_current') or 0
    inactive_file = stat.get('inactive_file', 0)
    slab_reclaimable = stat.get('slab_reclaimable', 0)
    return {
        'footprt': max(0, (current - inactive_file
                           - slab_reclaimable + swap) // 1024),
        'kcharge': current // 1024,
    }

