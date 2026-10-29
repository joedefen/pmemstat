> **Quick Start**: from the CLI (requires **Python 3.10 or later**)
> * **Install (preferred, per-user):** `pipx install pmemstat`
> * **Run:** `pmemstat` — runs as root via `sudo` to show *all* processes (your password may be required)
> * **No root capability?** `pmemstat -U` (or `--user`) — an account without root privilege opts out and sees only *its own* processes
> * **Upgrade:** `pipx upgrade pmemstat || pipx install pmemstat`
> * Type `?` within `pmemstat` for the help screen.

> **`pmemstat` runs as root by default; an unprivileged (`-U`) run is a degraded, partial view, not a smaller-but-equivalent one:**
> 1. **You see only your own processes.** Every other user's (and most of root's) memory and CPU is missing, so totals understate real usage; the `PIDs=shown/total` count makes the gap explicit.
> 2. **System-wide header numbers degrade.** The unaccounted residual `Oth` cannot be split into its kernel (`OthK`) and user (`OthU`) parts — it silently absorbs every process you cannot read, so it is inflated and misleading.
> 3. **Growth / leak monitoring is crippled.** The cross-run, per-boot history is root-only, so an unprivileged run measures growth only from *this run's start* (no "since boot" baseline), and the `z` zap reset is unavailable.
> 4. **Kill mode reaches only your own processes.**
>
> Use `-U` only when you deliberately want a user-only view (or cannot elevate); otherwise the default root run works as designed.


# pmemstat - Proportional Memory Status

![pmemstat animated demo](https://github.com/joedefen/pmemstat/blob/main/images/pmemstat.2026-10-29.14-43.gif?raw=true)

`pmemstat` shows detailed **proportional** memory use of **Linux** processes by digesting `/proc/{PID}/smaps` and `/proc/{PID}/smaps_rollup`.

Proportional memory splits the cost of shared memory among the processes sharing it (instead of counting it repeatedly, as `top` does) and excludes uninstantiated virtual memory.

Without `-o`, `pmemstat` is much faster but less detailed (it omits classes such as SysV shared memory, usually absent anyway). With `-o` it may take a few seconds to start, though loop-mode refreshes stay fast. In window mode, `o` toggles the detail.

Grouping rolls up the resources of multiple processes of one feature (e.g. a browser) so its total memory/CPU impact is apparent. Looping reveals growth that may be a leak, and segregating memory by type makes leaks easier to identify.

## How `pmemstat` Compares to Other Tools
Reach for `top`/`ps` when you need a tool that is always installed, and `htop` for a friendly full-screen overview. `pmemstat` differs in its focus on **accurate, proportional memory**: rather than RES/RSS — which double-counts shared pages and ignores uninstantiated virtual memory — it digests `smaps`/`smaps_rollup` to attribute shared memory fairly and break memory down by type. That makes it slower to start (especially with `-o`) but far more truthful about who is really using RAM. The table compares it with its closest peers: `smem` (the other tool that derives **PSS** from `smaps`) and `systemd-cgtop` (whose per-cgroup view `pmemstat` mirrors, with proportional numbers).

| Feature | `pmemstat` | `smem` | `systemd-cgtop` | `htop` |
|---|---|---|---|---|
| **Proportional memory (PSS)** accounting | ✓ core design | ✓ core design | ✗ (`memory.current`) | ✗ (RES, double-counts) |
| **Memory-type breakdown** (shared/swap/stack/text/data) | ✓ | ✗ (USS/PSS/RSS/Swap only) | ✗ | ✗ |
| **Proportional swap** per grouping | ✓ | ✗ (flat Swap column) | ✗ | ✗ (total swap only) |
| **Group processes** by executable/command | ✓ aggregates PIDs into one row | ✓ by command by default | ✗ | ✗ (tree view only) |
| **Group by cgroup v2** (services, scopes, containers) | ✓ (`-g cgroup`) | ✗ | ✓ (its core design) | ✗ |
| **Memory-leak / growth monitoring** (annotates top-N growers; only re-shows significant growth) | ✓ (`G`, `--growth-style`, growth sort) | ✗ | ✗ | ✗ |
| **zRAM-aware effective RAM** (eTot/eUsed/eAvail) | ✓ | ✗ | ✗ | ✗ |
| **System pressure (PSI)** in header (memory/cpu/io, some/full) | ✓ (`-P`) | ✗ | ✗ | ✗ |
| **Data source** | `/proc/{PID}/smaps*` | `/proc/{PID}/smaps` | `/sys/fs/cgroup` | `/proc` + libs |
| **Interactive full-screen window** | ✓ (curses) | ✗ (one-shot) | ✓ | ✓ |
| **CPU usage reporting** | ✓ | ✗ (memory only) | ✓ | ✓ |
| **Kill processes** | ✓ (inline confirm) | ✗ | ✗ | ✓ |
| **Inline search-as-you-type** | ✓ (`/`) | ✗ | ✗ | ✓ (F3) |
| **Requires root for all-process detail** | ✓ (else only your own) | ✓ (else only your own) | ✗ | ✗ |

For **per-mapped-file attribution** (which shared library or mmap'd file costs the most RAM), use `smem -m` or `pmap`; `pmemstat` deliberately stays focused on per-process and per-cgroup responsibility rather than rolling PSS up by mapped file.

## Installation Options
* `pmemstat` requires **Python 3.10 or later**.
* Root is needed to read `smaps`/`smaps_rollup` for **all** processes, so `pmemstat` runs as root via `sudo` by default. Persistent default flags go in `PMEMSTAT_ARGS` (e.g. `export PMEMSTAT_ARGS='--psi --loop 3 -s name'`); anything typed on the command line still takes precedence.
* If elevation fails (for example, no terminal for a password prompt and sudo not pre-authorized), the run exits with a clear message rather than silently downgrading.
* To opt out of elevation and run user-only, use `--user`/`-U`; the header marks such a run with `--user` so the degraded (partial) view is obvious.

See the **Quick Start** above for the basic install; **Alternative Installation Options** below covers system-wide and non-`pipx` installs.


## Usage
```
usage: pmemstat [-h] [-D] [-C] [-a CPU_AVG_SECS] [-P]
                [-g {exe,cmd,pid,cgroup,cgroupCharge}] [-f] [-k MIN_DELTA_KB]
                [-l LOOP_SECS] [-L CMDLEN] [-t TOP_PCT] [-U]
                [--save-history-now] [--reset-history-now] [-o]
                [-u {MB,KB,human}] [-s {mem,cpu,name,growth}]
                [--growth-style {off,both,growth,rate}] [--growth-top {all,10}]
                [--no-growth-history] [--dont-save-growth-history] [-/ SEARCH]
                [-W] [pids ...]

positional arguments:
  pids                  list of pids/groups (none means every accessible pid)

options:
  -h, --help            show this help message and exit
  -D, --debug           debug mode (the more Ds, the higher the debug level)
  -C, --no-cpu          do NOT report percent CPU (only in window mode)
  -a, --cpu-avg-secs CPU_AVG_SECS
                        CPU moving-average smoothing window in seconds [5-90,
                        dflt=20]
  -P, --psi             show PSI (system pressure in header + memPSI% column)
                        [dflt=off]
  -g, --groupby {exe,cmd,pid,cgroup,cgroupCharge}
                        grouping method for presenting rows; the cgroup
                        variants differ only in the memory view: "cgroup"
                        (footprt) or "cgroupCharge" (memory.current)
  -f, --fit-to-window   do not overflow window [if -w]
  -k, --min-delta-kb MIN_DELTA_KB
                        minimum delta KB to show again [dflt=100 if DB else
                        1000]
  -l, --loop LOOP_SECS  loop interval in secs [dflt=5 if -w else 0]
  -L, --cmdlen CMDLEN   max shown command length [dflt=36 if not -w]
  -t, --top-pct TOP_PCT
                        report group contributing to top pct of ptotal
                        [dflt=100]
  -U, --user            opt out of the default root elevation and run as user;
                        marks the header with a dimmed "--user" so the
                        degraded view is obvious
  --save-history-now    save the current stats for growth tracking and exit
                        (runs as root; ignores other options and the
                        environment)
  --reset-history-now   reset the saved growth history so all growth is
                        measured from the current values, then exit (runs as
                        root; ignores other options and the environment)
  -o, --others          expand "other" into shSYSV, shOth, stack, text
  -u, --units {MB,KB,human}
                        units of memory [dflt=MB]
  -s, --sortby {mem,cpu,name,growth}
                        sort method for presenting rows
  --growth-style {off,both,growth,rate}
                        leak/growth style: off|both|growth|rate [dflt=off]
  --growth-top {all,10}
                        annotate only the top-N growers: all|10 [dflt=all]
  --no-growth-history   do NOT seed growth from the saved history (fresh
                        baseline)
  --dont-save-growth-history
                        do NOT update the saved growth history
  -/, --search SEARCH   show items with search string in name
  -W, --no-window       show in "curses" window [disables: -D,-t,-L]

```
Explanation of some options and arguments:
* `-g {exe,cmd,pid,cgroup,cgroupCharge}, --groupby {...}` - select the grouping of memory stats for reporting: the executable basename (the default `exe`), the truncated command line (`cmd`, length via `-L`), one process (`pid`), or the cgroup v2 path (`cgroup`, showing the derived **footprint** total, or `cgroupCharge`, showing the kernel's `memory.current`); see "Grouping by cgroup v2" below.
* `-a CPU_AVG_SECS, --cpu-avg-secs CPU_AVG_SECS` - the CPU smoothing window in seconds, an integer from 5 to 90 (default 20), which damps brief spikes without hiding sustained load.
* `-P, --psi` - show PSI (off by default): adds a **system pressure (PSI)** table to the header *and* the per-cgroup `memPSI%` column; see "System pressure (PSI)" and "Grouping by cgroup v2" below.
* `-k MIN_DELTA_KB, --min-delta-kb MIN_DELTA_KB` - when looping, how much change in memory use is required to show the grouping in subsequent loops: a positive value means the group must **grow** by that amount (KB), a non-positive value means it need only **change** by that amount. It also gates the growth annotation (a row is annotated only if its absolute growth is at least `-k`).
* `--growth-style {off,both,growth,rate}` - the inline growth ("leak") annotation for the groups that are growing (default `off`); the `G` key cycles it in window mode. See "Memory growth (leak) detection" below.
* `--growth-top {all,10}` - annotate only the top-N growers by the displayed metric (default `all` = no cap).
* `--no-growth-history` / `--dont-save-growth-history` - opt out of (respectively) seeding growth from the saved history and updating it; `PMEMSTAT_NO_HISTORY` disables both. A non-root run (`-U`) always disables both. See "Memory growth (leak) detection" below.
* `-W, --no-window` - report once to the terminal instead of the interactive curses window (with `-l` it loops there too). Window mode (the default) forces `-t 100`, `-L 100` and off `-D`; `-W` forces `-C` (no CPU).
* `pids` - the positional arguments may be pids (i.e., numbers) or the names of executables (as shown by `-gexe`)


## Grouping by cgroup v2
With `-g cgroup` (or cycling `g` in window mode) `pmemstat` groups processes by their **cgroup v2** path (read from `/proc/<pid>/cgroup`), which corresponds to systemd services/scopes and to container sandboxes. Each row is labelled with the cgroup's leaf unit name (e.g. `foo.service`), and a trailing `+` marks a cgroup that also contains descendant cgroups listed on their own rows. If the leaf name matches none of the row's member executables — common with launcher-created app scopes (e.g. niri's `app-niri-fuzzel-*.scope`, which actually host the launched app) — the row is labelled with the dominant member executable instead; the full cgroup path stays searchable with `/`.

There are two cgroup memory views:
* `kcharge` - what the kernel charges, and what `memory.max` / the OOM killer act on; the number `systemd-cgtop`/`docker stats` show.
* `footprt` - what the cgroup really needs to keep resident: a stable working-set proxy that does not balloon with page cache and still accounts for swapped-out pages.

Because `pmemstat` computes **proportional** memory (PSS), `ptotal` is *not* the number `systemd-cgtop`, `docker stats` or `podman stats` report (they use the non-proportional `memory.current`, which over-counts shared pages). The cgroup groupings therefore keep `ptotal` as a left-hand **reference** column but use the kernel charge as the row total: `-g cgroup` shows `footprt`, `-g cgroupCharge` shows `memory.current` (`kcharge`).

The kernel columns, read from `/sys/fs/cgroup/<path>/` in the `-u` units, decompose that charge, and the slices to the left of the total sum to it:
* `anon` - anonymous memory charged to the cgroup
* `cache` - page cache (reclaimable inactive cache removed in the `footprt` view)
* `kmem` - kernel memory (reclaimable slab removed in the `footprt` view)
* `swap` - the kernel swap charge (`footprt` view only; swap is not part of `memory.current`). Distinct from `pswap` (the smaps-proportional swap shown in other modes)
* `oK` - the remainder, normally just `sock`, so the row adds up
* `footprt` / `kcharge` - the row **total** for the active grouping (`cgroup` -> `footprt`; `cgroupCharge` -> `kcharge`):
    * `footprt` (default) - the derived **footprint**: what the cgroup really needs to keep resident; also the growth metric
    * `kcharge` - `memory.current`, the kernel's own charge
* `memPSI%` - memory-pressure stall time as a percentage; shown only with `-P` (PSI), beside `cpu%`

These columns appear only in the cgroup groupings (`-g cgroup` / `-g cgroupCharge`), and are omitted entirely when the kernel exposes no cgroup v2 data (e.g. a cgroup v1 host).

cgroup v2 accounting is **hierarchical**: each cgroup's figures include its descendants, so a row with separately listed descendants (marked `+`) has their figures **subtracted** and shows only its own share; rows therefore add up to `TOTALS`. The unified root (`/`, labelled `(root)`) is everyone's ancestor, so a process living directly in it — as inside a container, where it may be the only cgroup visible — makes the `(root)+` row count every other row as a descendant. `memPSI%` is not totalled (`n/a` in `TOTALS` and `---- OTHERS ----`).

Reading `/proc/<pid>/cgroup` and `/sys/fs/cgroup` needs no root, so the grouping works unprivileged too (the PSS columns still reflect only the processes you can read).

## System pressure (PSI)
With `-P` (or the `P` key in window mode) pmemstat adds a small header table reporting Linux **Pressure Stall Information**, read from `/proc/pressure/{memory,cpu,io}`: the fraction of time work stalled waiting for a resource, so higher means more of a bottleneck.

```
       SOME 10s     60s    300s     FULL 10s     60s    300s
memPSI%    0.42    0.10    0.02         0.00    0.00    0.00
cpuPSI%    0.00    0.03    0.00            -       -       -
 ioPSI%    0.15    0.60    1.04         0.10    0.49    0.96
```
Each cell is the kernel's `avg10`/`avg60`/`avg300` stall-time percentage for its window. `SOME` means at least one task was stalled; `FULL` means **all** non-idle tasks were stalled, and is normally available only for `memory` and `io` (cells a resource does not provide show `-`). A resource this kernel does not provide is omitted; if no resource is readable, the table is not shown.

This is the **system-wide** view, distinct from the per-cgroup `memPSI%` column described under "Grouping by cgroup v2".

## Example Output
```
20:49:12 Tot=7.6G Used=6.2G Avail=1.4G OthK=512M OthU=3.6G Sh+Tmp=477M PIDs=174
     2.4%/ker MajF/s=2  zRAM=813M CR=4.3 eTot:16.8G eUsed:8.8G eAvail:8.0G
 cpu%      pswap   other    data  ptotal   key/info (exe by mem)
    60.8   2,535     593   3,988   7,116 T 174x --TOTALS in MB --
───────────────────────────────────────────────────────────────────────────────
     5.9   1,366      90   2,110   3,567   24x browser
    16.6      89     117     822   1,028   9x code
     5.9     270      32     291     593 3.5G 2h3m 1x firefox
     1.6      95      68     335     497   6x exe
     2.4     150      66      94     310   6x brave
    16.0      52      73     152     277   3x vivaldi
     0.1      39      19      13      70   2x konsole
```

This is the default **root** run; an unprivileged (`-U`) run shows a single `Oth` (see below).

In the default window loop we see
* a **leader line** with the current time, and `Tot`, `Avail`, `Used` (= Tot-Avail) and `Sh+Tmp` from `/proc/meminfo`, plus:
    * the unaccounted-for memory `Oth = Tot - Avail - Tmp - ptotal` — whatever is in neither `ptotal` (the sum of proportional process totals) nor `Sh+Tmp`: kernel, reserve, drivers, imprecision, etc., and when **not** root every process whose `smaps` you cannot read.
       * As **root** every process is accounted for, so `Oth` splits into **OthK** = `SUnreclaim + KernelStack + PageTables` (kernel-owned) and **OthU** = `Oth - OthK` (the userland remainder).
       * When **not root** other users' memory is folded into the residual, so a single **Oth** is shown; `PIDs=shown/total` on the same line is the clue that `ptotal` is incomplete.
       * BTRFS, ZFS, zRAM or unattached SysV shared memory can make `Oth` significant (and MemAvailable understated); start with `sudo slabtop -sc` and feature tools (e.g. `zpool list`).
    * contributing PIDs vs the total excluding kernel threads
* an optional **PSI block** (with `-P`), as described in "System pressure (PSI)" above
* a **second leader line for zRAM** (only if zRAM is active):
    * percent CPU consumed by kernel processes (not normalized: 4 CPUs can total 400%) — with zRAM mostly the swap process
    * **MajF/s** - major page faults per second; 100+/s means heavy swapping and memory pressure, 1000+/s is extreme
    * **zRAM** - "Used" RAM storing zRAM data
    * **eTot** / **eUsed** / **eAvail** - effective Total / Used / Available RAM (more accurate as **zRAM** grows)
* a **header line with the reported fields**:
    * **cpu** - percent CPU (again not normalized to 100%)
    * **pswap** - proportional swap use (per smaps_rollup)
    * **other** - partly sums these categories (shown by the key, `o`):
      * **shSYSV** - proportional System V shared memory use (per smaps)
      * **shOth** - proportional use of other shared memory (per smaps)
      * **stack** - exclusive stack use per smaps
      * **text** - proportional use of text memory (the read-only binary code, per smaps)
    * **data** - exclusive data ("heap") use per smaps
    * **ptotal** - proportional memory of all categories (sum of the columns to the left); with zRAM it includes **pswap**
    * *empty* - entry type: **T** (grand total), **A** (newly added grouping), **O** (overflow groupings below `--top-pct`) or **x** (a grouping whose processes have all ended; its last summary is kept briefly)
    * **growth** - inline growth annotation for top-N growers (e.g. `3.5G 2h3m`); only when growth mode is not `off`
    * **key/info** - **{num}x**, the number of processes in the grouping (e.g. `24x browser`), plus the grouping key; `pid` grouping rows show `1x`, the pid and the command line.

## Colour and Themes
In window mode colour highlights titles and values that are changing; it is
decoration only, so a terminal without colour (or `TERM=dumb`) still shows the
full report in plain text.

`t` opens the theme picker (`ESC` returns) and cycles the appearance. **`mono`**
turns colour off entirely, using bold and reverse-video attributes for emphasis.

The choice is remembered per user. `--theme` selects a theme, `--no-color` (or
the standard `NO_COLOR` convention) disables colour, `--theme mono` is the same
as `--no-color`, and `--list-themes` prints the available names.

## Memory growth (leak) detection

With `--growth-style` (or the `G` key in window mode) `pmemstat` annotates the
groups that are growing, making slow leaks visible without scrolling history:

```
     5.9     270      32     291     593 3.5G 2h3m 1x firefox
```

* The annotation is `{human growth} {interval}` (e.g. `3.5G 2h3m`); `rate` adds
  a per-day projection (`3.5G/d`) once the baseline spans at least a minute, and
  `growth` shows just the size.
* The column's `growth` label sits on the **TOTALS** row, directly above the
  annotations it titles; the TOTALS row's `T` marker is dropped there (its info
  already reads TOTALS) and returns when growth is `off`.
* Changing the grouping (`g`/`o`) rebuilds every row, which would mark them all
  new for one refresh. With growth `off` that `A` flash is kept (it explains the
  reshuffle), but in a growth mode the annotations are shown instead, from the
  per-view history that survives the change; a key that genuinely appears later
  still gets its `A`.
* Growth is measured from the group's **first observation this boot** (possibly
  an earlier run), not merely since this invocation; a reboot resets it. This
  history is system-wide and **root-only**: a non-root run (`-U`) measures only
  from this run's start. Opt out with `--no-growth-history`,
  `--dont-save-growth-history`, or `PMEMSTAT_NO_HISTORY` (both).
* Startup bursts age out, and growth is clamped at zero, so shrinking is not
  reported.
* A row is annotated only if its growth (KB) is at least `-k` **and** it is
  among the top-N growers (`--growth-top`, default `all`; `p` cycles `all`/`10`).
  `-s growth` sorts by the displayed metric.
* With a cgroup grouping the metric is the grouping's total (`footprt` for
  `-g cgroup`, `kcharge` for `-g cgroupCharge`) rather than proportional PSS.
* A system-wide line beneath the leader closes the identity
  `ΔUsed = ΔTOTALS(ptotal) + Δ(Sh+Tmp) + ΔOth`; as **root** the residual splits
  into `ΔOthK`/`ΔOthU`, so kernel/driver growth is separable from userspace and
  tmpfs, while a non-root run shows a single `ΔOth`. The shared baseline interval
  trails as `[1h58m]` in every growth mode.

## Updating History from Cron / Startup
`pmemstat --save-history-now` performs one silent scan, saves the current
history and exits (`0` on success, `1` on failure with a message on stderr). It
runs as root (elevating via `sudo` if needed) and ignores all other options and
`PMEMSTAT_ARGS`, so it is safe from cron or a startup unit. Run it as the user
whose history you want to update.

`pmemstat --reset-history-now` scans the same way but **replaces** the saved
history rather than merging: current values become the new baseline, as if the
tool had first run just after boot. It likewise runs as root and ignores other
options. Inside the window, `z` performs the same reset (only as root while the
growth annotation is on), asking for `y`/`Y` confirmation first; `ESC` or 10
seconds declines.

## Key Legend (Window Mode)
The top line of the header is an always-visible key legend with the live search
field appended, so the available keys are in evidence without opening the help
screen:

    ?:help group units sort cpu Kill PSI Growth /{regex}

`?` opens the complete list of keys plus the navigation keys (press it again, or
`ESC`, to return). `zap` is appended for a root run while the growth annotation
is showing, since resetting the baseline has no visible effect otherwise.

## Help Screen (in Window Mode, Press '?')
In window mode, `?` opens the help screen (and closes it again); **ESC** also
closes it. Navigation keys are at the top and, below the line, the option keys
with their possible values (the current choice is highlighted). Typing an option
key (e.g. `c`) advances to the next value, applied on the next loop; the option
keys also work directly on the main screen without opening the help screen. `t`
opens the theme picker (where `t` cycles the appearance), and `K` on this screen
toggles **demo mode**, which shows the last key pressed — handy for screencasts.
**ENTER** closes nothing here — it stays reserved for kill mode's row selection.

## Inline Search (Window Mode)
Press `/` to activate inline search mode; the live search field is the trailing
`/{regex}` on the first header line. The display filters in real-time,
case-insensitively, matching any part of the process info. **Enter** commits the
search; **ESC** cancels and clears it.

## Kill Mode (Window Mode)
Press `K` (on the report; on the Help screen `K` is demo mode instead) to enter
"Kill Mode" where you can select and kill processes:
1. Use the navigation keys to highlight a row and press **Enter**
2. An inline confirmation prompt names the process and its PID count; press **`y`** to kill the process(es) or **ESC** to cancel
3. A brief flash message reports the result
4. Press `K` again to exit Kill Mode

## Scroll Position (Window Mode)

Sometimes, the horizontal line between the header and scrollable region has a reverse video block (under the "351" in this case):
![scroll-pos example](https://github.com/joedefen/pmemstat/blob/main/images/scroll-pos.png?raw=true)

Its position and length indicate how far through the document you are and how
much of it is visible; with no block, the document does not overflow. Press `f`
to fit the document to the screen with a "rollup" line summarizing the lines
that would not fit.

## Quirks and Details
* **pswap** comes only from `smaps_rollup`, so it may be slightly out of sync with the `smaps` data.
* **ptotal** (from `smaps`) and **pss** (from `smaps_rollup`, usually hidden) differ more than expected, though still closely.
* After the first loop, refreshes are faster but slightly less detailed.
* the "exe" value comes from `/proc/{PID}/cmdline`: the leading path is stripped, and if the result is a script interpreter (python, perl, bash, ...) whose first argument is a full path, it is shown as `{interpreter}->{basename(script)}` (e.g. `python3->pmemstat.py`).

## Alternative Installation Options
The Quick Start's per-user `pipx` install is preferred. Variants:
* **System-wide with `pipx` (root)** — makes `pmemstat` available to every account and to plain `sudo pmemstat` (the launcher lands in `/usr/local/bin`):
```
        sudo pipx install --global pmemstat
        # to upgrade:   sudo pipx upgrade --global pmemstat || sudo pipx install --global pmemstat
        # to uninstall: sudo pipx uninstall --global pmemstat
```
* **From PyPi as non-root (`pip`)** — you need `~/.local/bin` on your `$PATH`:
```
        python -m pip install --user pmemstat
        # to uninstall: python -m pip uninstall pmemstat
```
* **From PyPi as root (`pip`)** — installs into `/usr/local/bin` for all users; `PIP_BREAK_SYSTEM_PACKAGES=1` may be required on some distros:
```
        sudo PIP_BREAK_SYSTEM_PACKAGES=1 python3 -m pip install pmemstat
        # to uninstall: sudo python -m pip uninstall pmemstat
```