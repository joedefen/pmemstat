> **Quick Start**: from the CLI (requires **Python 3.10 or later**)
> * **Install (preferred, per-user):** `pipx install pmemstat`
> * **Run:** `pmemstat` — shows only *your* processes
> * **Full view (all processes):** `pmemstat --sudo` re-runs itself as root. To avoid typing the argument every time, add `export PMEMSTAT_ARGS=--sudo` to your shell init (e.g. `~/.bashrc` or `~/.zshrc`), then just run `pmemstat`.
> * **Upgrade:** `pipx upgrade pmemstat || pipx install pmemstat`
> * Type "?" within `pmemstat` to show the help screen.

> **Note (v4.0.0 — breaking change):** `pmemstat` no longer re-runs itself as root automatically; in 3.x that was the default. Use `pmemstat --sudo` (or `PMEMSTAT_ARGS=--sudo`) for the full, all-process view.


# pmemstat - Proportional Memory Status

`pmemstat` shows detailed **proportional** memory use of **Linux** processes by digesting:
* `/proc/{PID}/smaps`
* `/proc/{PID}/smaps_rollup`

Computing proportional memory avoids overstating memory use as many programs do (e.g., `top`). Specifically, proportional memory splits the cost of common memory to the processes sharing it (rather than counting common memory multiple times). And, it does not include uninstantiated virtual memory. 

Without `-o`, `pmemstat` shows less detail and is much faster and sometimes less accurate (e.g., it will not report classes of memory such as SysV shared memory, which are often absent anyway). With `-o` providing full detail, digging out the numbers is slower; thus, `pmemstat -o` may take a few seconds to start, but, in its loop mode, refreshes are relatively fast and efficient by avoiding recomputing unchanged numbers. When in its window mode, typing `o` toggles low/high detail.

`pmemstat`'s grouping feature rolls up the resources of multiple processes of a feature (e.g., a browser) to make the total memory/cpu impact much more apparent.

Its looping features allow monitoring for changes in memory growth which may be "leaks".  Segregating memory by types can make identifying leaks faster and more certain.

## How `pmemstat` Compares to Other Tools
Reach for `top` or `ps` when you need a tool that is always installed, and `htop` when you want a friendly full-screen overview. Where `pmemstat` differs is its focus on **accurate, proportional memory**: instead of reporting each process's resident set size (RES/RSS) — which double-counts shared pages and ignores uninstantiated virtual memory — it digests `smaps`/`smaps_rollup` to attribute shared memory fairly and to break memory down by type. That makes `pmemstat` slower to start (especially with `-o`) but far more truthful about who is really using RAM. The table below compares it against its closest peers: `smem` (the other tool that derives **PSS** from `smaps`) and `systemd-cgtop` (whose per-cgroup view `pmemstat` now mirrors, but with proportional numbers).

Three of `pmemstat`'s capabilities are **new or substantially overhauled** in recent releases and are marked **🆕** in the table below: **cgroup v2 grouping**, **memory-leak / growth monitoring**, and **system pressure (PSI)**. They are the areas where `pmemstat` pushes well past its peers (all three are ✗ for `smem`, `systemd-cgtop` and `htop`), and each has its own section below.

| Feature | `pmemstat` | `smem` | `systemd-cgtop` | `htop` |
|---|---|---|---|---|
| **Proportional memory (PSS)** accounting | ✓ core design | ✓ core design | ✗ (`memory.current`) | ✗ (RES, double-counts) |
| **Memory-type breakdown** (shared/swap/stack/text/data) | ✓ | ✗ (USS/PSS/RSS/Swap only) | ✗ | ✗ |
| **Proportional swap** per grouping | ✓ | ✗ (flat Swap column) | ✗ | ✗ (total swap only) |
| **Group processes** by executable/command | ✓ aggregates PIDs into one row | ✓ by command by default | ✗ | ✗ (tree view only) |
| **Group by cgroup v2** 🆕 (services, scopes, containers) | ✓ (`-g cgroup`) | ✗ | ✓ (its core design) | ✗ |
| **Memory-leak / growth monitoring** 🆕 (annotates top-N growers; only re-shows significant growth) | ✓ (`G`, `--growth-style`, growth sort) | ✗ | ✗ | ✗ |
| **zRAM-aware effective RAM** (eTot/eUsed/eAvail) | ✓ | ✗ | ✗ | ✗ |
| **System pressure (PSI)** 🆕 in header (memory/cpu/io, some/full) | ✓ (`-P`) | ✗ | ✗ | ✗ |
| **Data source** | `/proc/{PID}/smaps*` | `/proc/{PID}/smaps` | `/sys/fs/cgroup` | `/proc` + libs |
| **Interactive full-screen window** | ✓ (`console-window`/curses) | ✗ (one-shot) | ✓ | ✓ |
| **CPU usage reporting** | ✓ | ✗ (memory only) | ✓ | ✓ |
| **Kill processes** | ✓ (inline confirm) | ✗ | ✗ | ✓ |
| **Inline search-as-you-type** | ✓ (`/`) | ✗ | ✗ | ✓ (F3) |
| **Requires root for all-process detail** | ✓ (else only your own) | ✓ (else only your own) | ✗ | ✗ |

In short: reach for `top`/`htop` when you want a fast, broad system overview, and reach for `pmemstat` when you need to know **how much memory a program (or group of programs) is truly responsible for**, to break that memory down by type or by cgroup, or to watch for creeping growth that suggests a leak. For **per-mapped-file attribution** (which shared library or mmap'd file costs the most RAM), use `smem -m` or `pmap`; `pmemstat` deliberately stays focused on per-process and per-cgroup responsibility rather than rolling PSS up by mapped file.

## Installation Options
* `pmemstat` requires **Python 3.10 or later**.
* `pmemstat` needs root privileges to read the memory statistics (`smaps`/`smaps_rollup`) for **all** processes; without them it reports only your own. Get the full view with `pmemstat --sudo`, or set `PMEMSTAT_ARGS=--sudo` once in your shell init to avoid typing it.
* `pmemstat` runs as the invoking user by default and never silently escalates to root.
* For a persistent set of default flags, export `PMEMSTAT_ARGS` with a shell-quoted argument string, e.g. `export PMEMSTAT_ARGS='--sudo --psi --loop 3 -s name'`. It is tokenized like a command line and prepended to the real arguments, so anything typed on the command line still takes precedence.
* When `--sudo` is requested but elevation cannot happen (no terminal for a `sudo` prompt and sudo not pre-authorized, or the install is not importable by the isolated interpreter), `pmemstat` fails with a clear message and a non-zero exit instead of silently downgrading to a user-only view. It never hangs waiting for a password.
* To force user-only operation and disable auto-elevation, use the `--run-as-user` or `-U` option.
* `pmemstat` depends on `console-window` (the curses UI), pinned to an **exact version** on purpose. Installing `pmemstat` pulls the pinned version automatically; do not "upgrade" `console-window` independently.

See the **Quick Start** above for the basic install. For system-wide/root or non-`pipx` installs, see **Alternative Installation Options** below.


## Usage
```
usage: pmemstat [-h] [-D] [-C] [-a CPU_AVG_SECS] [-P]
                [-g {exe,cmd,pid,cgroup,cgroupCharge}] [-f] [-k MIN_DELTA_KB]
                [-l LOOP_SECS] [-L CMDLEN] [-t TOP_PCT] [-U] [--sudo]
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
  -U, --run-as-user     run as user (NOT as root)
  --sudo                re-run self as root via sudo (or set
                        PMEMSTAT_ARGS=--sudo)
  --save-history-now    save the current stats to the history ledger and exit
                        (implies --sudo; ignores other options and the
                        environment)
  --reset-history-now   reset the history ledger so all growth is measured
                        from the current values, then exit (implies --sudo;
                        ignores other options and the environment)
  -o, --others          expand "other" into shSYSV, shOth, stack, text
  -u, --units {MB,KB,human}
                        units of memory [dflt=MB]
  -s, --sortby {mem,cpu,name,growth}
                        sort method for presenting rows
  --growth-style {off,both,growth,rate}
                        leak/growth style: off|both|growth|rate [dflt=off]
  --growth-top {all,10}
                        annotate only the top-N growers: all|10 [dflt=all]
  --no-growth-history   do NOT seed growth from the cross-run ledger (fresh
                        baseline)
  --dont-save-growth-history
                        do NOT write the cross-run growth ledger
  -/, --search SEARCH   show items with search string in name
  -W, --no-window       show in "curses" window [disables: -D,-t,-L]

```
Explanation of some options and arguments:
* `-g {exe,cmd,pid,cgroup,cgroupCharge}, --groupby {...}` -  select the grouping of memory stats for reporting.
    * `exe` - group by basename of the executable (the default)
    * `cmd` - group by the truncated command line (use `-L CMDLEN` to choose length)
    * `pid` - group by one process
    * `cgroup` - group by cgroup v2 path (services, scopes and containers), showing the derived **footprint** total; see "Grouping by cgroup v2" below
    * `cgroupCharge` - the same cgroup v2 grouping, but each row's slices and total show the kernel's `memory.current` charge instead of the derived footprint; see "Grouping by cgroup v2" below
* `-a CPU_AVG_SECS, --cpu-avg-secs CPU_AVG_SECS` - the CPU smoothing window in seconds, an integer from 5 to 90 (default 20). Each process's `cpu%` is averaged over this window so brief spikes are damped without hiding sustained load.
* `-P, --psi` - show PSI (off by default): adds a **system pressure (PSI)** table to the header *and* the per-cgroup `memPSI%` column; see "System pressure (PSI)" and "Grouping by cgroup v2" below. In window mode this can also be toggled with the `p` key.
* `-k MIN_DELTA_KB, --min-delta-kb MIN_DELTA_KB` - when looping, how much change in memory use is required to show the grouping in subsequent loops; note:
    * a positive `MIN_DELTA_KB` means the total memory of the grouping must **grow** by that amount (in KB)
    * a non-positive `MIN_DELTA_KB` means the total memory of the grouping must **change** by that amount (in KB)
    * it also gates the growth annotation: a row is annotated only if its absolute growth (in KB) is at least `-k`
* `--growth-style {off,both,growth,rate}` - the inline growth ("leak") annotation for the groups that are growing (default `off`); in window mode the `G` key cycles it. The baseline is geometric and self-forgetting, so startup bursts age out. See "Memory growth (leak) detection" below.
* `--growth-top {all,10}` - annotate only the top-N growers by the displayed metric (default `all` = no cap); in window mode the `t` key cycles `all`/`10`
* `--no-growth-history` / `--dont-save-growth-history` - opt out of (respectively) seeding growth from the cross-run ledger and writing it; `PMEMSTAT_NO_HISTORY` disables both. See "Memory growth (leak) detection" below.
* `-W, --no-window` - report once to the terminal instead of the interactive curses window (with `-l` it loops there too). Window mode (the default) forces `-t 100`, `-L 100` and off `-D`; `-W` forces `-C` (no CPU).
* `pids` - the positional arguments may be pids (i.e., numbers) or the names of executables (as shown by `-gexe`)


## Grouping by cgroup v2
With `-g cgroup` (or cycling `g` in window mode) `pmemstat` groups processes by their **cgroup v2** path (read from `/proc/<pid>/cgroup`), which corresponds to systemd services/scopes and to container sandboxes. Each row is labelled with the cgroup's leaf unit name (e.g. `foo.service`); systemd's `\xNN` escapes are decoded for readability, and a trailing `+` marks a cgroup that also contains descendant cgroups listed on their own rows. If the leaf name matches none of the row's member executables — common with launcher-created app scopes (e.g. niri's `app-niri-fuzzel-*.scope`, which actually host the launched app such as VS Code or Vivaldi) — the row is labelled with the dominant member executable instead; the full cgroup path stays searchable with `/`.

You can select either of two cgroup memory views, `kcharge` and `footprt`:

* `kcharge` answers "what does the kernel charge, and what does `memory.max` / the OOM killer act on?" — the verifiable `systemd-cgtop`/`docker stats` number.
* `footprt` answers "what does this cgroup really need to keep resident?" — a more stable working-set proxy that does not balloon just because the page cache filled up, and that still accounts for swapped-out pages.

Because `pmemstat` computes **proportional** memory (PSS), the `ptotal` column is *not* the same number that `systemd-cgtop`, `docker stats` or `podman stats` report: those use the kernel's `memory.current`, which is not proportional and over-counts pages shared between processes. For that reason the cgroup groupings keep `ptotal` as a left-hand **reference** column (the tool's namesake proportional total) but do **not** use it as the row total; the row total is the kernel charge, and the grouping chooses which charge: `-g cgroup` shows the derived `footprt`, `-g cgroupCharge` shows `memory.current` (`kcharge`).

The kernel columns, read from `/sys/fs/cgroup/<path>/` in the same units as every other column (`-u`), are a decomposition of that charge. The slices to the left of the total add up to it exactly:
* `anon` - `memory.stat`'s `anon`: anonymous memory charged to the cgroup
* `cache` - page cache: `file` (in the `kcharge` view) or `file - inactive_file` (in the `footprt` view, where reclaimable inactive cache is removed)
* `kmem` - kernel memory: `kernel` (`kcharge`) or `kernel - slab_reclaimable` (`footprt`)
* `swap` - the kernel swap charge (`footprt` view only; swap is not part of `memory.current`). Distinct from `pswap` (the smaps-proportional swap shown in other modes)
* `oK` - the remainder, `total - (anon + cache + kmem [+ swap])`; normally just `sock`, it guarantees the row adds up
* `footprt` / `kcharge` - the row **total** for the active grouping (`cgroup` -> `footprt`; `cgroupCharge` -> `kcharge`):
    * `footprt` (default) - the derived **footprint**: `memory.current - inactive_file - slab_reclaimable + swap`; also the growth metric
    * `kcharge` - `memory.current`, the kernel's own charge
* `memPSI%` - `memory.pressure`'s `some avg10`: memory-pressure stall time as a percentage; shown only with `-P` (PSI), beside `cpu%`

So the memory columns of a `-g cgroup` row read `ptotal | anon cache kmem [swap] oK | <total>`: the leftmost value is pmemstat's proportional total (for comparison), and the bracketed slice block sums to the view total on the right. The raw `memory.current` is visible in the `kcharge` view.

These columns appear only in the cgroup groupings (`-g cgroup` / `-g cgroupCharge`), and are omitted entirely when the kernel exposes no cgroup v2 data (e.g. a cgroup v1 host).

cgroup v2 accounting is **hierarchical**: a cgroup's `anon`/`cache`/`kmem`/`swap`/`footprt`/`kcharge` already include its descendant cgroups. A row whose cgroup also contains descendant cgroups listed separately is flagged with a trailing `+`, and **those descendants' figures are subtracted** so the row shows only that cgroup's own share. Consequently every row is its own share and the rows add up to the `TOTALS` row, and within each row the slice block adds up to the view total. The unified root (`/`, labelled `(root)`) is the ancestor of every cgroup, so if a process lives directly in it — as can happen inside a container, where it may be the only cgroup visible — that `(root)+` row counts every other row as its descendant. `memPSI%` is not totalled — it shows `n/a` in `TOTALS` and in the `---- OTHERS ----` row, since a sum of pressure percentages is meaningless.

Reading `/proc/<pid>/cgroup` and `/sys/fs/cgroup` does not require root, so the grouping and these numbers are available even in non-root mode (the proportional PSS columns still reflect only the processes you are permitted to read).

## System pressure (PSI)
With `-P` (or the `p` key in window mode) pmemstat adds a small (bold-headed) table to the header reporting Linux **Pressure Stall Information**, read from `/proc/pressure/{memory,cpu,io}`. PSI expresses the fraction of time that work was stalled waiting for a resource: the higher the number, the more that resource is a bottleneck.

```
       SOME 10s     60s    300s     FULL 10s     60s    300s
memPSI%    0.42    0.10    0.02         0.00    0.00    0.00
cpuPSI%    0.00    0.03    0.00            -       -       -
 ioPSI%    0.15    0.60    1.04         0.10    0.49    0.96
```
* The heading row names the two stall groups, `SOME` and `FULL`, and the three 10/60/300 **second** window columns (`10s`/`60s`/`300s`), which are right-aligned over the values below.
* Each resource row (`memPSI%`, `cpuPSI%`, `ioPSI%`) holds the kernel's `avg10`/`avg60`/`avg300` stall-time percentage for that window.
* `SOME` - at least one task was stalled; `FULL` - **all** non-idle tasks were stalled. `FULL` is normally available only for `memory` and `io`; a resource that provides no `full` line (for example `cpu.pressure` on many kernels) shows `-` in its `FULL` cells.
* A resource whose file is absent (a kernel built with `CONFIG_PSI=n`) is simply omitted; if no resource is readable, the table is not shown.
* This is the **system-wide** view. It is distinct from the per-cgroup `memPSI%` column described under "Grouping by cgroup v2" (which is one cgroup's `memory.pressure some avg10`).

## Example Usage with Explanation of Output
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

In the default refreshed window loop, we see
* a **leader line** with:
    * the current time
    * from `/proc/meminfo`: MemTotal (Tot), MemAvailable (Avail), Used (Tot-Avail) and Shmem+tmpfs (Sh+Tmp).
    * 'Oth' is the unaccounted for memory belonging to the kernel, reserve,
       drivers, imprecision, etc.; `Oth = Tot - Avail - Tmp - ptotal`.
       * Features such as BTRFS, ZFS, zRAM, unattached SysV Shared Memory, etc., can cause 'Oth' to be significant;
         that is, MemAvail is often greatly understated (and 'Oth' is overstated).  As a test (not intended for regular use), try:
         `sudo sync; sudo sh -c "echo 3 > /proc/sys/vm/drop_caches"`; if that reduces `Oth` significantly,
         then the MemAvailable calculation was off significantly.
       * Determining the contributors can be difficult, but start with `sudo slabtop -sc` and feature specific tools (e.g., `zpool list`).
    * how many PIDs are contributing to the report vs the total number of PIDs excluding kernel threads
* an optional **PSI block** (only with `-P`) as described in "System pressure (PSI)" above
* a **second leader line for zRAM** only if zRAM is active with:
    * percent cpu consumed by kernel processes (not normalized ... if there are 4 CPUs, then the total CPU can 400%). This is important because, with zRAM, this often is mostly the swap process.
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
    * **data** - exclusive use of memory for data (i.e., exclusive use of "heap" memory, per smaps)
    * **ptotal** - proportional use of memory of all categories (i.e., sum of columns to the left); **note**, if zRAM, this includes **pswap**, else not.
    * *empty* - type of the entry which may be:
        * **T** - the grand total
        * **A** - a newly added grouping
        * **O** - combined overflow groupings below the `--top-pct` threshold (on the first loop, or always in window mode)
        * **{growth} {interval}** - inline growth annotation for the top-N growers (e.g. `3.5G 2h3m`), shown only when the growth mode is not `off` (`G` in window mode)
    * **key/info** - a quantifier plus the grouping key. The quantifier is **{num}x**, the number of processes in the grouping (e.g. `24x browser`); in the `pid` grouping each row shows `1x` followed by the pid and command line.
        
## Memory growth (leak) detection

With `--growth-style` (or the `G` key in window mode) `pmemstat` annotates the
groups that are growing, making slow leaks visible without scrolling history:

```
     5.9     270      32     291     593 3.5G 2h3m 1x firefox
```

* The annotation is `{human growth} {interval}` (e.g. `3.5G 2h3m`); the `rate`
  mode shows a per-day projection (`/d`, e.g. `3.5G/d`) once the baseline
  interval is at least a minute, and `growth` shows just the size.
* The baseline comes from a **per-boot history ledger** (see "Updating History
  from Cron / Startup") that `pmemstat` writes as it runs and on exit, so growth
  is measured from the group's **first observation this boot** — possibly an
  earlier run — rather than merely since this invocation began; a reboot resets
  it. Pass `--no-growth-history` to instead measure only from this run's start,
  `--dont-save-growth-history` to stop writing the ledger, or
  `PMEMSTAT_NO_HISTORY` to disable both.
* Within a run the baseline is **geometric and self-forgetting**: a group's
  first 16 seconds never count, so startup bursts age out. Growth is clamped at
  zero (a reduction is not reported).
* A row is annotated only if its growth (KB) is at least `-k` **and** it is
  among the top-N growers (`--growth-top`, default `all`; `t` cycles
  `all`/`10`). The `G` key cycles `off -> both -> growth -> rate -> off`.
* `-s growth` sorts rows by the displayed growth metric (rate in `both`/`rate`).
* With a cgroup grouping the metric is that grouping's total (`footprt` for
  `-g cgroup`, or `kcharge` for `-g cgroupCharge`; see "Grouping by cgroup v2")
  rather than proportional PSS.
* A system-wide line is shown beneath the leader (same mode) and closes the
  accounting identity `ΔUsed = ΔTOTALS(ptotal) + Δ(Sh+Tmp) + ΔOthK + ΔOthU`,
  where `OthK = SUnreclaim + KernelStack + PageTables` and `OthU` is the
  remainder, so kernel/driver growth (`ΔOthK`/`ΔOthU`) is separable from
  userspace (`ΔTOTALS`) and tmpfs (`Δ(Sh+Tmp)`). The shared baseline interval
  is printed once, as a trailing `[1h58m]`, in **every** growth mode
  (`both`/`growth`/`rate`); the brackets distinguish it from the last
  (`ΔOthU`) value and, in `rate` mode, give the window the `/d` projection was
  drawn from.
* Caveats: `Oth*` are only meaningful when run as root; when not root the `OthU`
  remainder also includes other users' memory.

## Updating History from Cron / Startup
Run `pmemstat --save-history-now` to perform a single **silent** scan and write
the per-boot history ledger, then exit: return code `0` on success, `1` on
failure (with an explanation on stderr; nothing on success). It implies
`--sudo` and ignores every other option and `PMEMSTAT_ARGS`, so it is safe from
cron or a system-startup unit, e.g. `pmemstat --save-history-now`. The ledger is
per invoking user (`/tmp/pmemstat-<uid>`, or `PMEMSTAT_STATE_DIR` if set), so run
it as the user whose history you want to keep; a root cron job without
`SUDO_UID` updates the root ledger instead.

Run `pmemstat --reset-history-now` for the same silent scan but with the ledger
**replaced** rather than merged: the current system and per-group values become
the new baseline, exactly as if the tool had first run just after this boot, so
every later growth annotation and delta is measured from that moment onward. It
likewise implies `--sudo`, ignores the other options and `PMEMSTAT_ARGS`, and
returns `0` on success / `1` on failure (nothing on success). Inside the window,
pressing `z` (shown as `[z]ap` in the key legend, and only offered when running
as root) performs the same reset at run time: the in-memory growth anchors are
cleared and the ledger is rewritten from the current values.

## Key Legend (Window Mode)
The top line of the header is an always-visible key legend (dimmed, left-aligned
under the leader's `Tot=` value, shifted 5 columns left so the optional `[z]ap`
entry below still leaves room for the live search field) with the live search
field appended in bold, so the available keys are in evidence without opening
the help screen:

    [?]help [g]roup [u]nits [s]ort [c]pu [K]ill [p]SI [G]rowth /{regex}

When running as root, `[z]ap` is appended for the run-time history reset:

    [?]help [g]roup [u]nits [s]ort [c]pu [K]ill [p]SI [G]rowth [z]ap /{regex}

`?` remains the gateway to the complete list of keys plus the navigation keys.
Because `?` is listed first, a narrow terminal truncates only the least-critical
trailing entries, never the way to the full help screen.

## Help Screen (in Window Mode, Press '?')
In window mode, press '?' to enter the help screen, which looks like:

```
-- HELP SCREEN ['?' or ENTER closes Help; Ctrl-C exits ] --
Navigation:      H/M/L:      top/middle/end-of-page
  k, UP:  up one row             0, HOME:  first row
j, DOWN:  down one row           $, END:  last row
  Ctrl-u:  half-page up     Ctrl-b, PPAGE:  page up
  Ctrl-d:  half-page down     Ctrl-f, NPAGE:  page down
──────────────────────────────────────────────────────────────────────────────
Key: z - zap the growth history (current values become the new baseline)
Type keys to alter choice:
? - help screen····················  normal help
K - kill mode······················  off ON
                                   :  Select line + ENTER to kill selected
f - fit rows to window·············  off ON
g - group by·······················  exe cmd pid cgroup cgroupCharge
o - less category detail···········  off ON
s - sort by························  mem cpu name growth
G - leak/growth mode···············  off both growth rate
t - top-N growers··················  all 10
u - memory units···················  MB KB human
c - show cpu·······················  off ON
p - show PSI (header + memPSI%)····  off ON
```

> **Note:** in the live window the *current* choice of each option is shown in reverse video (e.g. `help`, `off`); which value is highlighted depends on your live state, so it may differ from the text above.

**Notes:**
* There are a number of navigation keys (mostly following vim conventions); in the help screen, they apply to the help screen; otherwise, they apply to the main screen.
* Below the line, there are a number of keys/options; when you type an option key (e.g. "c"), it highlights the next option value (e.g. "off"); changed options are applied on the next loop of the main screen.
    * These option keys can be used in the main screen too (e.g. pressing "c" hides or reveals the CPU column without entering the help screen).
    
## Inline Search (Window Mode)
Press `/` to activate inline search mode. The live search field is the trailing `/{regex}` on the first header line (next to the key legend):
* As you type, the display filters in real-time to show only matching processes
* While editing, the field is shown in **reverse video** with a `|` cursor; after Enter the committed pattern stays visible in bold
* Press **Enter** to finalize the search
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
* **pswap** seems to be only provided by the `smaps_rollup` file, and thus it may be slightly out of sync with the data gathered by `smaps`.
* the **ptotal** (from 'smaps') and **pss** (from `smaps_rollup` and usually hidden) seem to differ more than expected but still close.
* after the first loop, **pss** is read and only groups with sufficient aggregate change are probed for the details.  Thus, subsequent loops are more efficient by avoiding the reading of `smaps` in many cases (with some loss of accuracy).
* the "exe" value comes from the command line (based on `/proc/{PID}/cmdline`, which is a bit funky). Firstly, the leading path is stripped; secondly, if the resulting executable is a script interpreter (e.g., python, perl, bash, ...) AND the first argument seems to be a full path (i.e., starts with "/"), then the "exe" is shown as "{interpreter}->{basename(script)}" (e.g. `python3->pmemstat.py`).


## Test Program and Test Suggestions
The C program, `memtest.c` is included and can be compiled by running `cc memtest.c -o memtest`.  This program:
* regularly allocates more memory of all types -- about **3 MiB per loop** by default (SysV shared memory, a memory-mapped file and the heap, plus a little stack),
* can be run several times simultaneously,
* will share SysV shared memory and memory mapped files,
* bounds its own growth so it will not push the system into the OOM killer (the total is capped near `min(256 MiB, RAM/16)`).

Because the default growth per loop (~3 MiB) exceeds `pmemstat`'s default `-k` gate (1000 KB), the test program stays visible from loop to loop and -- with `--growth-style` enabled and `--growth-top all` -- is annotated as a grower **without** having to relax `-k`. Options to tune it:
* `-q` - quick: sleep 1s between loops instead of 10s
* `-s` - short: only 16 loops
* `-n LOOPS` - number of loops
* `-c KiB` - bytes added to each pool per loop (default 1024 KiB)
* `-b MiB` - total memory budget (default `min(256 MiB, RAM/16)`)

With the more aggressive default you no longer need `-uKB`/`-k{small-number}`; those remain useful only when you deliberately shrink the test with `-c`.

Running a number of sleeps of various durations in the background in a loop, plus one foreground sleep, can make for a robustness test with lots of processes coming and going.  There are many "races" (i.e., a process may appear in `/proc`, but its `smaps` is gone), and this test helps ensure the races are handled properly.

## Alternative Installation Options
The Quick Start's per-user `pipx` install is preferred. Variants:
* **System-wide with `pipx` (root)** — makes `pmemstat` visible to every account and lets plain `sudo pmemstat` work, because the launcher lands in `/usr/local/bin` (which `sudo` searches):
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
* **From PyPi as root (`pip`)** — makes `pmemstat` available to all users with `/usr/local/bin` on `$PATH`. Note: `PIP_BREAK_SYSTEM_PACKAGES=1` may be required on some distros:
```
        sudo PIP_BREAK_SYSTEM_PACKAGES=1 python3 -m pip install pmemstat
        # to uninstall: sudo python -m pip uninstall pmemstat
```