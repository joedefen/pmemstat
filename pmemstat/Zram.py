"""zRAM effective-memory projection."""
import os
from types import SimpleNamespace

from pmemstat.Format import human


# ###################################################################################
# PORTABLE FUNCTION - Copy-pasted from zram-advisor When updating:
# copy entire function from zram_advisor/main.py (lines 86-166)
# ###################################################################################
def compute_zram_effective(meminfo_total, meminfo_used, meminfo_available,
                          zram_orig_data_size, zram_mem_used_total,
                          zram_disksize, zram_mem_limit=0, limit_pct=80):
    """

    Compute effective memory values accounting for zRAM compression.

    This is a PORTABLE function that can be copy-pasted between projects.
    NO class dependencies - all inputs passed as parameters.

    Args:
        meminfo_total: Total physical RAM in bytes
        meminfo_used: Currently used RAM in bytes
        meminfo_available: Available RAM in bytes
        zram_orig_data_size: Uncompressed data size stored in zRAM (bytes)
        zram_mem_used_total: Physical RAM consumed by zRAM including overhead (bytes)
        zram_disksize: Max uncompressed data zRAM will accept (bytes)
        zram_mem_limit: Max RAM zRAM can use (0=unlimited) (bytes)
        limit_pct: Arbitrary limit on % of RAM zRAM should use (default 80%)

    Returns:
        SimpleNamespace with:
            - e_used: Effective memory used (uncompressed equivalent)
            - e_avail: Effective memory available
            - e_max_used: Maximum effective memory at full zRAM capacity
            - ratio_current: Current effective compression ratio
            - ratio_projected: Projected ratio accounting for degradation
            - projection_confidence: 'low', 'medium', or 'high'
            - usage_fraction: Fraction of disksize currently used
    """
    # Calculate effective used memory (what it would be if uncompressed)
    e_used = meminfo_used - zram_mem_used_total + zram_orig_data_size

    # Determine compression ratio
    ratio = None
    if e_used <= meminfo_used:  # zRAM not helping yet
        e_used = meminfo_used
        ratio = 2.75  # Conservative guess for projection when no data yet

    if ratio is None:  # Calculate actual ratio
        ratio = zram_orig_data_size / zram_mem_used_total if zram_mem_used_total > 0 else 2.75

    # Apply memory limit if configured
    if zram_mem_limit > 0:
        stats_limit_pct = (zram_mem_limit / meminfo_total) * 100
        limit_pct = min(100.0, limit_pct, stats_limit_pct)

    # Calculate usage fraction and apply degradation
    usage_fraction = zram_orig_data_size / zram_disksize if zram_disksize > 0 else 0

    # Degradation logic based on current usage
    if usage_fraction < 0.10:
        degradation_factor = 0.75  # Aggressive: little real data
        confidence = "low"
    elif usage_fraction < 0.50:
        degradation_factor = 0.85  # Moderate: some real data
        confidence = "medium"
    else:
        degradation_factor = 0.95  # Conservative: lots of real data
        confidence = "high"

    projected_ratio = ratio * degradation_factor

    # Project maximum effective memory when zRAM fills to limit
    e_max_used = projected_ratio * meminfo_total * limit_pct / 100
    # Cap by disksize limit
    e_max_used = min(e_max_used, zram_disksize)
    # Add uncompressed memory not in zRAM
    e_max_used += meminfo_total - e_max_used / projected_ratio

    # Calculate effective available
    e_avail = e_max_used - e_used

    return SimpleNamespace(
        e_used=e_used,
        e_avail=e_avail,
        e_max_used=e_max_used,
        ratio_current=ratio,
        ratio_projected=projected_ratio,
        projection_confidence=confidence,
        usage_fraction=usage_fraction
    )

# ###################################################################################
# ##### ZramProjector class
# ###################################################################################
class ZramProjector:
    """ Guess the zram numbers """
    def __init__(self):
        self.meminfo = None
        self.e_total = 0
        self.e_avail = 0
        self.e_used = 0
        self.e_max_used = 0
        self.ratio = 0.0
        self.ratio_projected = 0.0
        self.projection_confidence = 'low'
        self.limit_pct = 80
        self.devs = {}
        self.DB = False

    def human_pct(self, number, with_pct=False):
        """ Return the number in human form and as pct total memory. """
        rv = human(number)
        if with_pct and number > 0 and self.meminfo.MemTotal > 0:
            fraction = number/self.meminfo.MemTotal
            pct = int(round(100*fraction))
            rv += f'/{pct}%'
        return rv

    def _get_zram_stats(self):
        """ Get only what we want
         orig_data_size   uncompressed size of data stored in this disk.
         compr_data_size  compressed size of data stored in this disk
         mem_used_total   the amount of memory allocated for this disk.
         mem_limit        max RAM ZRAM can use to store (0 means unlimited)
         mem_used_max     max RAM zram has consumed to store the data
        """
        fields = ('orig_data_size compr_data_size mem_used_total'
                 + ' mem_limit mem_used_max').split()
        infos = {}
        zram_devices = sorted([device for device in os.listdir('/sys/class/block/')
                    if device.startswith('zram')])
        for device in zram_devices:
            pathname = f'/sys/class/block/{device}/mm_stat'
            if not os.path.exists(pathname):
                continue # not active
            with open(pathname, encoding='utf-8') as fh:
                ns = SimpleNamespace()
                for line in fh: # all the goodies are on 1st line
                    nums = line.split()
                    for idx, field in enumerate(fields):
                        setattr(ns, field, int(nums[idx]))
                    break
                infos[device] = ns
            for param in ('disksize', ):
                pathname = f'/sys/class/block/{device}/{param}'
                with open(pathname, encoding='utf-8') as fh:
                    for line in fh: # one value, one line
                        setattr(ns, param, int(line.strip()))
                        break
            if self.DB:
                print(f'DB: {device}: {ns}')

        self.devs = infos
        return infos

    def compute_effective(self, meminfoKB):
        """ Compute the effective values - now uses portable function """
        def seed(meminfoKB):
            nonlocal self
            meminfo = self.meminfo = SimpleNamespace()
            for key, value in meminfoKB.items():
                setattr(meminfo, key, value*1024)
            # artificial stats
            meminfo.MemUsed = meminfo.MemTotal - meminfo.MemAvailable
            meminfo.MemZram=0
            self.e_total = meminfo.MemTotal
            self.e_avail = meminfo.MemAvailable
            self.e_used = self.e_total - self.e_avail
            self.e_max_used = self.e_used
            return meminfo

        meminfo = seed(meminfoKB)
        devs = self._get_zram_stats()
        if not devs:
            return

        # Aggregate stats from all zRAM devices
        stats = None
        for stat in devs.values():
            # NOTE: generalization for more than one
            # weak unless all are identically configured
            if not stats:
                stats = vars(stat).copy()
                continue
            for key, value in vars(stat).items():
                stats[key] += value
        stats = SimpleNamespace(**stats)
        self.meminfo.MemZram = stats.mem_used_total

        # Call the portable computation function
        result = compute_zram_effective(
            meminfo_total=meminfo.MemTotal,
            meminfo_used=meminfo.MemUsed,
            meminfo_available=meminfo.MemAvailable,
            zram_orig_data_size=stats.orig_data_size,
            zram_mem_used_total=stats.mem_used_total,
            zram_disksize=stats.disksize,
            zram_mem_limit=stats.mem_limit,
            limit_pct=self.limit_pct
        )

        # Update instance variables from result
        self.e_used = result.e_used
        self.e_avail = result.e_avail
        self.e_max_used = result.e_max_used
        self.ratio = result.ratio_current
        self.ratio_projected = result.ratio_projected
        self.projection_confidence = result.projection_confidence

