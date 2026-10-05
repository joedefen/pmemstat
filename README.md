> **Quick Start**: from the CLI (requires **Python 3.10 or later**)
> * **Preferred: install system-wide with `pipx`** — works as root or as your user, for every account:
>   * `sudo pipx install --global pmemstat`
>   * the launcher lands in `/usr/local/bin` (which `sudo` searches), so plain `sudo pmemstat` just works
>   * to upgrade: `sudo pipx upgrade --global pmemstat || sudo pipx install --global pmemstat`
> * **Fallback: per-user `pipx`** if you cannot install system-wide:
>   * `pipx upgrade pmemstat || pipx install pmemstat`
>   * the launcher lands in `~/.local/bin`, which `sudo` does **not** search; run it as your user: `pmemstat`
>   * for system-wide coverage, either install system-wide (above) or let it elevate itself: set `export PMEMSTAT_AUTO_SUDO=1` once, then just run `pmemstat`
> * **To run** (running as root shows **all** processes; as your user, only your own):
>   * `sudo pmemstat`  (system-wide install)
>   * `pmemstat`  (per-user install; your processes only, unless `PMEMSTAT_AUTO_SUDO=1`)
>   * type "?" within `pmemstat` to show the help screen.

> **Note (v4.0.0 — breaking changes):** the install method changed (prefer a system-wide `pipx install --global`; a per-user install is not elevated by `sudo`), and `pmemstat` no longer re-runs itself as root by default. For the 3.x behavior (full, all-process view when launched as your user), set `PMEMSTAT_AUTO_SUDO=1` in your environment — or just run `sudo pmemstat`.


# pmemstat - Proportional Memory Status

`pmemstat` shows detailed **proportional** memory use of **Linux** processes by digesting:
* `/proc/{PID}/smaps`
* `/proc/{PID}/smaps_rollup`

Computing proportional memory avoids overstating memory use as many programs do (e.g., `top`). Specifically, proportional memory splits the cost of common memory to the processes sharing it (rather than counting common memory multiple times). And, it does not include uninstantiated virtual memory. 

Without `-o`, `pmemstat` shows less details and is much faster and sometimes less accurate (e.g., it will not report classes of memory such as SysV shared memory which are often are not present anyhow). With `-o` providing full detail, digging out the numbers is slower; thus, `pmemstat -o` may take a few seconds to start, but, in its loop mode, refreshes are relatively fast and efficient by avoiding recomputing unchanged numbers. When in its window mode, typing `o` toggles low/high detail.

`pmemstat`'s grouping feature rolls up the resources of multiple processes of a feature (e.g., a browser) to make the total memory/cpu impact much more apparent.

Its looping features allow monitoring for changes in memory growth which may be "leaks".  Segregating memory by types can make identifying leaks faster and more certain.

**In version 3.x, `pmemstat` has these features**:
* In its **window mode**, `pmemstat` updates the terminal in place (using "curses") rather than scrolling.
* Showing **CPU use**, too, which makes `pmemstat` a viable alternative to `top` for regular use (although more specialized and still focused on accurate memory representation).
* Supports **inline search** (press `/` to search-as-you-type with live filtering).
* Supports **killing processes** with inline confirmation (press `y` to confirm, ESC to cancel).
* And several **new options** that can be **controlled dynamically** if in window mode.

## How `pmemstat` Compares to Other Tools
`top` and `htop` are excellent general-purpose process viewers, and `ps` is the classic snapshot tool. Where `pmemstat` differs is its focus on **accurate, proportional memory**: instead of reporting each process's resident set size (RES/RSS) — which double-counts shared pages and ignores uninstantiated virtual memory — it digests `smaps`/`smaps_rollup` to attribute shared memory fairly and to break memory down by type. That makes `pmemstat` slower to start (especially with `-o`) but far more truthful about who is really using RAM.

| Feature | `pmemstat` | `top` | `htop` | `ps` |
|---|---|---|---|---|
| **Proportional memory (PSS)** accounting | ✓ core design | ✗ (RES, double-counts) | ✗ (RES, double-counts) | ✗ (RSS) |
| **Memory-type breakdown** (shared/swap/stack/text/data) | ✓ | ✗ | ✗ | ✗ |
| **Proportional swap** per grouping | ✓ | ✗ | ✗ (total swap only) | ✗ |
| **Grouping** of processes by exe/cmd | ✓ aggregates PIDs into one row | ✗ | ✗ (tree view only) | ✗ (manual `--sort`/`grep`) |
| **Memory-leak / delta monitoring** (only re-shows significant growth) | ✓ (`-k`, loop mode) | ✗ | ✗ | ✗ |
| **zRAM-aware effective RAM** (eTot/eUsed/eAvail) | ✓ | ✗ | ✗ | ✗ |
| **Data source** | `/proc/{PID}/smaps*` | `/proc` summary | `/proc` + libs | `/proc` |
| **Interactive full-screen window** | ✓ (`console-window`/curses) | ✓ | ✓ | ✗ (one-shot) |
| **CPU usage reporting** | ✓ | ✓ | ✓ | ✓ |
| **Kill processes** | ✓ (inline confirm) | ✓ | ✓ | ✗ |
| **Inline search-as-you-type** | ✓ (`/`) | ✗ | ✓ (F3) | ✗ |
| **Requires root for all-process detail** | ✓ (else only your own) | ✗ | ✗ | ✗ |

In short: reach for `top`/`htop` when you want a fast, broad system overview, and reach for `pmemstat` when you need to know **how much memory a program (or group of programs) is truly responsible for**, to break that memory down by type, or to watch for creeping growth that suggests a leak.

## Installation Options
Note that:
* `pmemstat` requires **Python 3.10 or later**.
* `pmemstat` needs root privileges to read the memory statistics (`smaps`/`smaps_rollup`) for **all** processes; without them it reports only your own processes.
* By default `pmemstat` runs as the invoking user and shows a hint; it does **not** silently escalate to root.
* Where it is installed matters: a system-wide install (e.g. `sudo pipx install --global pmemstat`) is visible to both your user and root, so `sudo pmemstat` works; a per-user install lives in `~/.local/bin`, which root does not search, so you either run it as your user or let it elevate itself.
* To have it re-run itself under `sudo` automatically, opt in once with `export PMEMSTAT_AUTO_SUDO=1` in your shell profile, or pass `--auto-sudo` per invocation (for a system-wide install you can also simply run `sudo pmemstat`).
* Auto-elevation is skipped when there is no terminal for a `sudo` prompt and sudo is not already authorized, so scripts/CI never hang waiting for a password.
* To force user-only operation and disable auto-elevation, use the `--run-as-user` or `-U` option.
* `pmemstat` depends on `console-window`, pinned to an **exact version** on purpose. That package provides the curses UI and is deliberately not allowed to float: the pin protects against unexpected upstream changes and preserves the author's freedom to make backwards-incompatible UI revisions. Installing `pmemstat` pulls the pinned version automatically; do not "upgrade" `console-window` independently.

See the Quick Start at the top for preferred install instructions using `pipx`. If not acceptable, see the "Alternative Installation Options" section below.


## Usage
```
usage: pmemstat [-h] [-D] [-C] [-g {exe,cmd,pid}] [-f] [-k MIN_DELTA_KB]
        [-l LOOP_SECS] [-L CMDLEN] [-t TOP_PCT] [-n] [-U] [--auto-sudo] [-o]
        [-u {MB,mB,KB,human}] [-R] [-s {mem,cpu,name}] [-/ SEARCH] [-W] [pids ...]

positional arguments:
  pids                  list of pids/groups (none means every accessible pid)

options:
  -h, --help            show this help message and exit
  -D, --debug           debug mode (the more Ds, the higher the debug level)
  -C, --no-cpu          do NOT report percent CPU (only in window mode)
  -g {exe,cmd,pid}, --groupby {exe,cmd,pid}
                        grouping method for presenting rows
  -f, --fit-to-window   do not overflow window [if -w]
  -k MIN_DELTA_KB, --min-delta-kb MIN_DELTA_KB
                        minimum delta KB to show again [dflt=100 if DB else 1000]
  -l LOOP_SECS, --loop LOOP_SECS
                        loop interval in secs [dflt=5 if -w else 0]
  -L CMDLEN, --cmdlen CMDLEN
                        max shown command length [dflt=36 if not -w]
  -t TOP_PCT, --top-pct TOP_PCT
                        report group contributing to top pct of ptotal [dflt=100]
  -n, --numbers         show line numbers in report
  -U, --run-as-user     run as user (NOT as root)
  --auto-sudo           re-run self as root via sudo (same as PMEMSTAT_AUTO_SUDO)
  -o, --others          expand "other" into shSYSV, shOth, stack, text
  -u {MB,mB,KB,human}, --units {MB,mB,KB,human}
                        units of memory [dflt=MB]
  -R, --no-rise         do NOT raise change/adds to top (only in window mode)
  -s {mem,cpu,name}, --sortby {mem,cpu,name}
                        sort method for presenting rows
  -/ SEARCH, --search SEARCH
                        show items with search string in name
  -W, --no-window       show in "curses" window [disables: -D,-t,-L]

```
Explanation of some options and arguments:
* `-g {exe,cmd,pid}, --groupby {exe,cmd,pid}` -  select the grouping of memory stats for reporting.
    * `exe` - group by basename of the executable (the default)
    * `cmd` - group by the truncated command line (use `-L CMDLEN` to choose length)
    * `pid` - group by one process
* `-k MIN_DELTA_KB, --min-delta-kb MIN_DELTA_KB` - when looping, how much change in memory use is required to show the grouping in subsequent loops; note:
    * a positive `MIN_DELTA_KB` means the total memory of the groupin must **grow** by that amount (in KB)
    * a non-positive `MIN_DELTA_KB` means the total memory of the grouping must **change** by that amount (in KB)
* `pids` - the positional arguments may be pids (i.e., numbers) or the names of executables (as shown by `-gexe`) 


## Example Usage with Explanation of Output
```
20:49:12 Tot=7.6G Used=6.2G Avail=1.4G Oth=0 Sh+Tmp=477.7M PIDs=174
     2.4%/ker MajF/s=2  zRAM=813.2M CR=4.3 eTot:16.8G eUsed:8.8G eAvail:8.0G
 cpu_pct   pswap   other    data  ptotal   key/info (exe by mem)
    60.8   2,535     593   3,988   7,116 T 174x --TOTALS in MB --
───────────────────────────────────────────────────────────────────────────────
     5.9   1,366      90   2,110   3,567   24x browser
    16.6      89     117     822   1,028   9x code
     5.9     270      32     291     593   1x firefox
     1.6      95      68     335     497   6x exe
     2.4     150      66      94     310   6x brave
    16.0      52      73     152     277   3x VQ6B1EUZqoCU04zoRU
     0.1      39      19      13      70   2x konsole
```

In the default refreshed window loop, we see
* a **leader line** with:
    * the current time
    * from `/proc/meminfo` in MemTotal (Tot), MemAvailable (Avail), Tmp (Shmem+TmpFS), and Dirty.
    * 'Oth' is the unaccounted for memory belonging to the kernel, reserve,
       drivers, imprecision, etc.; `Oth = Tot - Avail - Tmp - ptotal`.
       * Features such as BTRFS, ZFS, zRAM, unattached SysV Shared Memory, etc., can cause 'Oth' to be significant;
         that is, MemAvail is often greatly understated (and 'Oth' is overstated).  As a test (not intended for regular use), try:
         `sudo sync; sudo sh -c "echo 3 > /proc/sys/vm/drop_caches"`; if that reduces `Oth` significantly,
         then the MemAvailable calculation was off significantly.
       * Determining the contributors can be difficult, but start with `sudo slabtop -sc` and feature specific tools (e.g., `zpool list`).
    * how many PIDs are contributing to the report vs the total number of PIDs excluding kernel threads
* a **second leader line for zRAM** only if zRAM is active with:
    * percent cpu consumed by kernel processes (not normalized ... if there 4 CPUs, then the total CPU can 400%). This is important because, with zRAM, this often is mostly the swap process.
    * **MajF/s** - the number of "major" page faults per second.
      * 100+/s is high/worrisome.	Indicates significant memory pressure and heavy swapping or repeated large file access. System thrashing/slowdown is likely noticeable slow response.
      * 1000+/s is extreme.	Indicates severe memory overcommitment, likely with processes constantly fighting for RAM, or an application aggressively accessing disk-mapped memory.
    * **zRAM** - the amount of "Used" RAM that is storing zRAM data
    * **eTot** - the effective Total RAM ... the bigger **zRAM**, the more accurate this number
    * **eUsed** - the effective Used RAM
    * **eAvail** - the effective Available RAM
* a **header line with the reported fields** including:
    * **cpu** - percent CPU (again not normalized to 100%)
    * **pswap** - proportional use of swap (per smaps_rollup)
    * **other** - partly sums these categories (shown by the key, `o`)
      * **shSYSV** - proportional use of System V shared memory (per smaps)
      * **shOth** - proportional use of other shared memory (per smaps)
      * **stack** - exclusive use of stack memory per smaps
      * **text** - proportional use of memory for text (i.e., the read-only binary code, per smaps)
    * **data** - exclusive use of memory for data (i.e., exclusively used of "heap" memory, per smaps)
    * **ptotal** - proportional use of memory of all categories (i.e, sum of columns to the left); **note**, if zRAM, this includes **pswap**, else not.
    * *empty* - type of the entry which may be:
        * **T** - the grand total
        * **A** - a newly added grouping
        * **O** - combined overflow groupings below the `--top-pct` threshold (only on first loop)
        * **+-{number}K** - number of KB of change in **ptotal** (only on subsequent loops)
    * **key/info** which is a quantifier plus the grouping key. The quantifier may be:
        * **{PID}** - when the grouping line represents one process (for option `-gexe`).
        * **{num}x** - where {num} is the number of processes in the grouping.
        
## Help Screen (in Window Mode, Press '?')
In window mode, press '?' to enter the help screen which looks like:

![helpscreen example](https://github.com/joedefen/pmemstat/blob/main/images/help-screen.png?raw=true)

**Notes:**
* There are a number of navigation keys (mostly following vim conventions); in the help screen, they apply to help screen; otherwise, they apply to main screen.
* Below the line, there are a number of keys/options; when you type an option key (e.g, "c"), it will highlight the next option value (e.g., "off"); when you change options, they will be applied to the next loop of the main menu.
    * These option keys can be used in the main menu (e.g., pressing "c" will change hide or reveal the CPU column w/o entering the help screen).
    
## Inline Search (Window Mode)
Press `/` to activate inline search mode. The search bar appears in the header on the right side:
* As you type, the display filters in real-time to show only matching processes
* The search pattern is shown in **reverse video** with a `|` cursor indicator
* Press **Enter** to finalize the search (pattern stays visible in normal video)
* Press **ESC** to cancel and clear the search
* The search is case-insensitive and matches any part of the process info

## Kill Mode (Window Mode)
Press `K` to enter "Kill Mode" where you can select and kill processes:
1. Use navigation keys to highlight a row
2. Press **Enter** to initiate kill
3. An inline confirmation prompt appears below the selected process:
   - Shows the process name and count of PIDs (e.g., `[3 PIDs]`)
   - Press **`y`** to confirm and kill the process(es)
   - Press **ESC** to cancel
4. After killing, a brief flash message shows the result (e.g., `✓ Killed 3 processes`)
5. Press `K` again to exit Kill Mode

## Scroll Position (Window Mode)

Sometimes, the horizontal line between the header and scrollable region has a reverse video block (under the "351" in this case):
![scroll-pos example](https://github.com/joedefen/pmemstat/blob/main/images/scroll-pos.png?raw=true)

**Notes:**
* When there is no block, the scrolled document does not overflow the scrollable region.
* When there is a block, its position indicates:
    * **Leftmost** - At the top of the document.
    * **Rightmost** - At the bottom of the document.
    * **Between Leftmost and Rightmost** - At percentage of the document approximated by its position from Left (0%) to Right (100%).
* Its length indicates, roughly, how much of the entire document you can see.
* Again, you can press 'f' to fit the document to the screen with a "rollup" line summarizing the lines that would not fit.

## Quirks and Details
* **pswap** seems to be only provided by the `smaps_rollups` file, and thus it may be slightly out of sync with the data gathered by `smaps`.
* the **ptotal** (from 'smaps') and **pss** (from `smaps_rollups` and usually hidden) seem differ more than expected but still close.
* after the first loop, **pss** is read and only groups with sufficient aggregate change are probed for the details.  Thus, subsequent loops are more efficient by avoiding the reading of `smaps` in many cases (with some loss of accuracy).
* the "exe" value comes from the command line (based `/proc/{PID}/cmdline` which is a bit funky). Firstly, the leading path is stripped; secondly, if the resulting executable is a script interpreter (e.g., python, perl, bash, ...) AND the first argument seems to be a full path (i.e., starts with "/"), then the "exe" will be represented as "{interpreter}->{basename(script)}".  For example, "python3->pmemstat.py" in the example above.


## Test Program and Test Suggestions
The C program, `memtest.c` is included and can be compiled by running `cc memtest.c -o memtest`.  This program:
* regularly allocates more memory of all types
* can be run several times simultaneously,
* will share SysV shared memory and memory mapped files,

When running `pmemstat` to monitor its memory use and changes, you should use `-uKB` and `-k{small-number}` so that you can "see" the very modest memory use of the test program and its changes.

Running a number of sleeps of various durations in the background in a loop, plus one foreground sleep, can make for a robustness tests with lots of processes coming and going.  There are many "races" (i.e., a process may appear in `/proc`, but its`smaps` is gone), and this test helps ensure the races are handled properly.

## Alternative Installation Options
If the `pipx` install is not acceptable, choose the best way to install:
* **From PyPi as non-root**. You need `~/.local/bin` on your `$PATH`.
```
        python -m pip install --user pmemstat
        # to uninstall: python -m pip uninstall pmemstat
```
* Or **from PyPi as root**. This makes `pmemstat` available to all users with `/usr/local/bin` on `$PATH`. Note: `PIP_BREAK_SYSTEM_PACKAGES=1` may be required on some distros.
```
        sudo PIP_BREAK_SYSTEM_PACKAGES=1 python3 -m pip install pmemstat
        # to uninstall: sudo python -m pip uninstall pmemstat
```