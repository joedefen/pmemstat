"""Per-process smaps/rollup collection and summarization."""
import os
import re
import traceback
from types import SimpleNamespace

from pmemstat import Debug
from pmemstat.CGroup import parse_cgroup_lines
from pmemstat.CpuSmooth import CpuSmooth


# ###################################################################################
# ##### ProcMem class
# ###################################################################################
class ProcMem:
    """Represents the memory map summation for processes and groups.
      - the ProcMem object represents one process (or pid)
      - the ProcMem static data represents aggregate data for groups.
    """
    section_pat = re.compile(
            r'^([0-9a-f]+)-([0-9a-f]+)' # $1,$2: 00400000-004b8000
            + r'\s+([a-z-]+)'   # $3: r-xp
            + r'\s+([0-9a-f]+)'  # $4: 00000000
            + r'\s+(\S+)'  # $5: fd:00
            + r'\s+(\d+)'  # $6: 11143998
            + r'(\s*|\s+(\S.*))$' # $8: /.../inetrep
            , re.IGNORECASE)
    item_pat = re.compile(
            r'^(\w+):' # $1: MMUPageSize:
            + r'\s+(\d+)'  # $2: 4
            + r'\s+kb$'  # kB
            , re.IGNORECASE)
    junk_pat = re.compile(
            r'^(THPeligible|VmFlags|ProtectionKey)'
            , re.IGNORECASE)
    opts = None
    # debug = 0 summaries = {} # indexed by pid TODO remove this (replace
    # by groups) prcs = {} groups = {} # indexed by group key (e.g.,
    # cmd) divisor = 0 # determined by arguments
    # units = '' # determined by arguments fwidth = 11
    pmemstat = None # the main program object
    max_cmd_len = 96 # command line maximum length
    chunk_dict = {
            'cat': None,
            'beg': 0,
            'end': 0,
            'offset': 0,
            'size': 0,
            'eSize': 0,
            'rss': 0,
            'pss': 0,
            'shared': 0,
            'private': 0,
            'swap': 0,
            'pswap': 0,
            'perms': '',
            'item': '',
            }
    clock_tick = None
    parse_err_cnt = 0

    def __init__(self, pid):
        self.pid = pid
        self.alive = True
        self.is_new = True
        self.wanted = True # until proven otherwise
        self.kernel = False # until proven otherwise
        self.is_changed = False
        self.why_not = None # populate me with why unwanted
        self.smaps_file = f'/proc/{self.pid}/smaps'
        self.rollup_file = f'/proc/{self.pid}/smaps_rollup'
        self.cpu = None
        self.exebasename = None, None
        self.key, self.cmdline, self.cmdline_trunc = None, None, None
        self.cgroup_path = None # cached cgroup v2 path (when grouping by cgroup)
        self.cgroup_read = False # whether the cgroup path has been read yet
        self.view_keys = None   # {grouping: key} for every supported view
        self.rollup = None      # last rollup summary (for view growth tracking)

    def refresh_cpu(self):
        """Get the Cpu Number for the PID (if possible)"""
        if not self.cpu:
            self.cpu = CpuSmooth(self.pid, avg_secs=getattr(
                ProcMem.opts, 'cpu_avg_secs', 20))
        return self.cpu.refresh_cpu() # sets self.cpu.percent

    def _get_exebasename(self, exepath, wds):
        """

        Final, definitive, robust executable base name resolver.
        """

        # --- Phase 1: Determine the Initial Best Name Candidate ---
        # ...  (same initial logic as before, using os.readlink)
        basename = os.path.basename(exepath)
        is_bad_name = (not basename or basename == 'exe' or len(basename) > 16
                       or not re.search(r'[a-zA-Z]', basename))

        if is_bad_name:
            try:
                link_target = os.readlink(f'/proc/{self.pid}/exe')
                if ' (deleted)' not in link_target and 'a.out' not in link_target:
                    basename = os.path.basename(link_target)
            except (FileNotFoundError, OSError):
                pass

        # --- Phase 2: Multi-Step Aggressive Cleanup (The FINAL Final Fix) ---

        # 1. Remove VQ... style hashes and generic suffixes
        hash_and_bin_pattern = r'(VQ[A-Z0-9]{10,}|-bin|\.bin)'
        basename = re.sub(hash_and_bin_pattern, '', basename).strip()

        # 2.  **CRITICAL FIX:** Remove all tab/PID/browser-related arguments from the tail.
        # This addresses both 'browser 14 tab' AND any
        # simple trailing numbers (like 'browser 4')
        tab_and_num_pattern = r'(?:\s+\d+)?\s+(tab.*|socket.*|rdd.*|process.*|\d+)$'
        basename = re.sub(tab_and_num_pattern, '', basename, flags=re.IGNORECASE).strip()

        # 3. Remove common internal browser-role suffixes (renderer, rdd, content, etc.)
        roles_pattern = r'-(renderer|gpu|utility|plugin|content|chrm|bsp|extension)$'
        basename = re.sub(roles_pattern, '', basename, flags=re.IGNORECASE).strip()

        # Final safety cleanup for any leading/trailing punctuation/spaces leftover
        basename = re.sub(r'^\W+|\W+$', '', basename).strip()

        # --- Phase 3: Last Resort Fallback ---

        if not basename or basename in ('exe', 'a.out'):
            try:
                basename = os.path.basename(wds[0])
            except IndexError:
                basename = 'exe'

        # Special Case Protection: Restore I3 if it was reduced
        if basename == 'i':
            basename = 'i3'

        return basename

    def get_cmdline(self):
        """Get the command line of the PID."""

        try:
            cmdline_file = f'/proc/{self.pid}/cmdline'
            try:
                # Closed explicitly: this runs once per PID per scan,
                # so a leaked handle here would accumulate for the life of the process.
                with open(cmdline_file, encoding='utf-8') as handle:
                    line = handle.read()[:-1]
            except FileNotFoundError as exc:
                if Debug.DebugLevel:
                    Debug.DB(1, f'skip pid={self.pid} no-rollup-lines exc={type(exc).__name__}')
                return

            arguments = line.split('\0')
            if not arguments or not arguments[0]: # kernel process
                self.wanted, self.kernel = False, True
                return

            exepath = arguments[0]

            # Prepare wds (words/arguments) for use in the helper and cmdline rebuild
            base_name_list = os.path.basename(exepath).split()
            if base_name_list:
                wds = base_name_list[1:] + arguments[1:]
            else:
                wds = arguments[1:]

            # Use the robust helper to determine the final, clean basename
            self.exebasename = self._get_exebasename(exepath, wds)

            # --- START ELABORATION LOGIC ---

            # 1. Elaboration: The Sudo Wrapper
            if self.exebasename == 'sudo' and wds:
                actual_cmd_name = os.path.basename(wds[0])
                if actual_cmd_name:
                    self.exebasename = actual_cmd_name
                    del wds[0]

            # 2. Elaboration: The Interpreter (Python, Perl, etc.)
            if self.exebasename in ('python', 'python2', 'python3', 'perl', 'bash', 'ruby',
                    'sh', 'ksh', 'zsh') and wds:

                # 2.a. Catch the 'python -m <module>' pattern (The New Fix)
                if len(wds) >= 2 and wds[0] == '-m':
                    module_name = wds[1]
                    self.exebasename = f'{self.exebasename}->{module_name}'
                    # Remove '-m' and the module name from arguments
                    del wds[0]
                    del wds[0]

                # 2.b. Catch the standard '<script.py>' pattern (Original Logic)
                elif wds[0] and os.path.exists(wds[0]) and os.path.isfile(wds[0]):
                    script = os.path.basename(wds[0])
                    if script != wds[0]:
                        # Only elaborate if it's a clear script name, not a generic main
                        if not re.search(r'^__main__\.', script):
                            self.exebasename = f'{self.exebasename}->{script}'
                            del wds[0] # Remove the script path from arguments

            # --- END ELABORATION LOGIC ---

            self.cmdline = ' '.join([self.exebasename] + wds)
            self.cmdline_trunc = self.cmdline[0:ProcMem.max_cmd_len]

        except Exception as exc:
            # ... (rest of exception handling is the same)
            print(f'  WARNING: skip pid={self.pid} no-basename exc={exc}')
            print(traceback.format_exc())
            self.wanted = self.kernel = False
            self.why_not = 'CannotGetCmdline'
            return

        # filter unwanted before too much work
        if (ProcMem.opts.pids and str(self.pid) not in ProcMem.opts.pids
                and self.exebasename not in ProcMem.opts.pids):
            self.wanted = False
            self.why_not = 'FilteredByArgs'

        self.set_key()

    def read_cgroup(self):
        """Read and cache this process's cgroup v2 path (``None`` if absent)."""
        if self.cgroup_read:
            return self.cgroup_path
        self.cgroup_read = True
        try:
            with open(f'/proc/{self.pid}/cgroup', encoding='utf-8') as fhandle:
                lines = fhandle.read().splitlines()
        except OSError as exc:
            if Debug.DebugLevel:
                Debug.DB(3, f'pid={self.pid} no-cgroup exc={type(exc).__name__}')
            return None
        self.cgroup_path = parse_cgroup_lines(lines)
        return self.cgroup_path

    def set_key(self):
        """Compute the grouping key for this process, for every view."""
        cgroup = self.read_cgroup() or '(no-cgroup)'
        self.view_keys = {
            'exe': self.exebasename,
            'cmd': self.cmdline_trunc,
            'pid': self.pid,
            'cgroup': cgroup,
            'cgroupCharge': cgroup,
        }
        self.key = self.view_keys[ProcMem.opts.groupby]

    def read_lines(self, filename):
        """ Get the lines of the smaps """
        lines = None
        try:
            with open(filename, encoding='utf-8') as fhandle:
                lines = fhandle.read().splitlines()
        except (PermissionError, FileNotFoundError) as exc:
            # normal cases: not permitted or this is a race where the pid is terminating
            self.why_not = f'CannotReadLines({type(exc).__name__})'
        except Exception as exc:
            # unexpected cases (probably a bug)
            if not self.opts.window:
                print(f'ERROR: skip pid={self.pid}',
                      f'no-smaps-or-rollup-lines exc={type(exc).__name__}')
            self.why_not = f'CannotReadLines({type(exc).__name__})'
        return lines

    def get_rollup_lines(self):
        """Get the lines of the 'smaps_rollup' file for this PID"""
        rollup_lines = []
        try:
            rollup_lines = self.read_lines(self.rollup_file)
        except Exception as exc:
            rollup_lines = []
            if Debug.DebugLevel:
                Debug.DB(1, f'skip pid={self.pid} no-rollup-lines exc={type(exc).__name__}')

        if not rollup_lines:
            self.wanted = False
            self.why_not = 'CannotReadRollups'
        elif Debug.DebugLevel:
            Debug.DB(3, f'pid={self.pid} {self.exebasename} #rollup_lines={len(rollup_lines)}')

        return rollup_lines

    def get_smaps_lines(self):
        """Get the lines of the 'smaps' file for this PID"""
        smaps_lines = []
        try:
            smaps_lines = self.read_lines(self.smaps_file)
        except Exception as exc:
            smaps_lines = []
            if Debug.DebugLevel:
                Debug.DB(1, f'skip pid={self.pid} no-smap-lines exc={type(exc).__name__}')

        if not smaps_lines:
            self.wanted = False
            self.why_not = 'CannotReadSmaps'
        else:
            if Debug.DebugLevel:
                Debug.DB(1, f'pid={self.pid} {self.exebasename} #smaps_lines={len(smaps_lines)}')
        return smaps_lines

    def make_chunks(self, lines):
        """ Parse the already smaps read lines."""
        chunks = []
        chunk = None
        for idx, line in enumerate(lines):
            match = self.section_pat.match(line)
            if match:
                if chunk:
                    chunks.append(chunk)
                chunk = SimpleNamespace(**ProcMem.chunk_dict)
                chunk.beg = int(match.group(1), 16)
                chunk.end = int(match.group(2), 16)
                chunk.perms = match.group(3)
                chunk.offset = int(match.group(4), 16)
                chunk.item = match.group(8)
                continue
            match = self.item_pat.match(line)
            if match:
                tag = match.group(1)
                val = int(match.group(2))
                if tag == 'Size':
                    chunk.size = val
                elif tag == 'Rss':
                    chunk.rss = val
                elif tag.startswith('Shared'):
                    chunk.shared += val
                elif tag.startswith('Private'):
                    chunk.private += val
                elif tag == 'Swap':
                    chunk.swap = val
                elif tag == 'Pss':
                    chunk.pss = val
                continue
            match = self.junk_pat.match(line)
            if match:
                continue
            if not self.parse_err_cnt:
                print(f'ERROR: cannot parse "{line}" [{self.smaps_file}:{idx+1}]')
            self.parse_err_cnt += 1
        if chunk:
            chunks.append(chunk)
        return chunks

    @staticmethod
    def make_summary_dict(pid=0, info=''):
        """ Make an object to summarize memory use of a PID or group """
        summary = {
                'cpu%': 0,
                'memPSI%': 0,  # cgroup v2 memory.pressure some avg10 (%)
                'pswap': 0,
                'shSYSV': 0,
                'shOth': 0, # e.g., memory mapped file
                'stack': 0,
                'text': 0,
                'data': 0, # deprecated 'pseudo' (e.g., memory barrier) now in 'data'
                'ptotal': 0,
                # cgroup v2 kernel-charge slices (see README "Grouping by cgroup v2").
                # anon/cache/kmem/swap partition the charge,
                # oK is the remainder (total - slices,
                # normally just sock) so the row adds up,
                # and kcharge/footprt are the two view totals.
                'anon': 0,     # cgroup v2 anon (KB)
                'cache': 0,    # cgroup v2 page cache (KB): file or file-inactive_file
                'kmem': 0,     # cgroup v2 kernel (KB): kernel or kernel-slab_reclaimable
                'swap': 0,     # cgroup v2 kernel swap charge (KB), footprt view
                'oK': 0,       # cgroup v2 remainder (KB): total - slices
                'kcharge': 0,  # cgroup v2 memory.current (KB)
                'footprt': 0,  # cgroup v2 derived footprint (KB): current - reclaimable + swap
                'pss': 0,  # comes from rollups
                'number': -pid if pid else 0, # count if positive; else -pid
                'info': info,
                }
        return summary

    def parse_rollups(self, lines):
        """ Parse the already read lines."""
        summary = ProcMem.make_summary_dict()
        for idx, line in enumerate(lines):
            if not line.endswith('kB'):
                continue
            match = self.item_pat.match(line)
            if match:
                tag = match.group(1)
                val = int(match.group(2))
                if tag == 'Pss_Anon':
                    summary['data'] += val
                    summary['ptotal'] += val
                elif tag == 'Pss_File':
                    summary['text'] += val
                    summary['ptotal'] += val
                elif tag == 'Pss_Shmem':
                    summary['shOth'] += val
                    summary['ptotal'] += val
                elif tag == 'SwapPss':
                    summary['pswap'] += val
                    if self.pmemstat.has_zram():
                        summary['ptotal'] += val
                continue
            print(f'ERROR: cannot parse "{line}" [{self.rollup_file}:{idx+1}]')
        summary['pss'] = summary['ptotal'] # for consistency
        return summary

    def categorize_chunks(self, chunks):
        """ Analyze the chunks to categorize the memory """
        for idx, chunk in enumerate(chunks):
            chunk.eSize = chunk.size
            if chunk.cat: # if already done, don't do again
                continue

            if 's' in chunk.perms:
                if 'SYSV' in chunk.item:
                    chunk.cat = 'shSYSV'
                    # chunk.eSize = chunk.rss + chunk.swap
                    chunk.eSize = chunk.pss
                else:
                    chunk.cat = 'shOth'
                    chunk.eSize = chunk.pss
            elif chunk.item and '[stack]' in chunk.item:
                chunk.cat = 'stack'
                chunk.eSize = chunk.private
            elif (chunk.size == 4 and idx < len(chunks) - 1
                    and chunk.offset == chunk.beg and not chunk.item
                    and '---p' in chunk.perms):
                    # stack seems to be 4K unwriteable immediately
                    # followed by something very huge like 10240 or 10236.
                    # The size is bogus ...
                    # replace the 'Size' with the 'Private' plus swapped
                nchunk = chunks[idx+1]
                if (chunk.end == nchunk.end
                        and 'w' in nchunk.perms
                        and not nchunk.item
                        and nchunk.offset == nchunk.beg
                        and nchunk.size >= 10000
                        and nchunk.size <= 20000):
                    chunk.eSize = 0
                    chunk.cat = 'data' # was 'pseudo'
                    nchunk.eSize = nchunk.private + nchunk.swap
                    nchunk.cat = 'stack'
            if not chunk.cat:
                if '---' in chunk.perms:
                    chunk.cat = 'data' # was 'pseudo'
                    chunk.eSize = 0
                elif 'w' in chunk.perms:
                    chunk.cat = 'data'
                    chunk.eSize = chunk.rss + chunk.swap
                else:
                    chunk.cat = 'text'
                    chunk.eSize = chunk.pss + chunk.swap
        if Debug.DebugLevel:
            for chunk in chunks:
                Debug.DB(6, '{self.pid} {self.exebasename} CHUNK:', chunk)

    def summarize_chunks(self, chunks):
        """ Accumulate the chunks into the summary of memory use for the PID """
        summary = self.make_summary_dict(self.pid)

        for chunk in chunks:
            if Debug.DebugLevel:
                Debug.DB(5, f'{self.pid} {self.exebasename} BLK: {chunk.cat} eSize={chunk.eSize}'
                    + f' size={chunk.size} {chunk.perms} {chunk.item}')
            summary[chunk.cat] += chunk.eSize
            summary['ptotal'] += chunk.eSize
            summary['pswap'] += chunk.pswap
        # print(f'DB self.summaries[{key}]: {self.summaries[key]}')
        return summary

    def prc_pid(self):
        """Process one PID"""
        self.alive = True
        self.is_changed = False
        self.rollup = None
        if not self.why_not and not self.cmdline:
            self.get_cmdline()
            if not self.cmdline:
                return
        rollup_lines = []
        if not self.why_not:
            rollup_lines = self.get_rollup_lines()
        if self.why_not:
            Debug.DB(4, f'pid={self.pid} {self.exebasename} why_not={self.why_not}')
            return
        self.is_changed = False
        rollup_summary = self.parse_rollups(rollup_lines)
        self.rollup = rollup_summary
        if self.opts.cpu:
            rollup_summary['cpu%'] = self.cpu.percent
        group = self.pmemstat.get_group(self.key)
        if not group.alive:
            info = str(self.key)
            if ProcMem.opts.groupby == 'pid':
                info += ' ' + self.cmdline_trunc
            group.rollup_summary = ProcMem.make_summary_dict(info=info)
            group.summary = ProcMem.make_summary_dict(info=info)
            group.alive = True
        self.pmemstat.add_to_summary(rollup_summary, group.rollup_summary)
        group.prcset.add(self)
