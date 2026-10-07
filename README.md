> **Quick Start**: from the CLI (requires **Python 3.10 or later**)
> * **Preferred: install system-wide with `pipx`** — works as root or as your user, for every account:
>   * `sudo pipx install --global pmemstat`
>   * the launcher lands in `/usr/local/bin` (which `sudo` searches), so plain `sudo pmemstat` just works
>   * to upgrade: `sudo pipx upgrade --global pmemstat || sudo pipx install --global pmemstat`
> * **Fallback: per-user `pipx`** if you cannot install system-wide:
>   * `pipx upgrade pmemstat || pipx install pmemstat`
>   * the launcher lands in `~/.local/bin`, which `sudo` does **not** search; run it as your user: `pmemstat`
>   * for system-wide coverage, either install system-wide (above) or let it elevate itself: set `export PMEMSTAT_ARGS=--sudo` once, then just run `pmemstat`
> * **To run** (running as root shows **all** processes; as your user, only your own):
>   * `sudo pmemstat`  (system-wide install)
>   * `pmemstat`  (per-user install; your processes only, unless `PMEMSTAT_ARGS=--sudo`)
>   * type "?" within `pmemstat` to show the help screen.

> **Note (v4.0.0 — breaking changes):** the install method changed (prefer a system-wide `pipx install --global`; a per-user install is not elevated by `sudo`), and `pmemstat` no longer re-runs itself as root by default. For the 3.x behavior (full, all-process view when launched as your user), set `PMEMSTAT_ARGS=--sudo` in your environment — or just run `sudo pmemstat`.


# pmemstat - Proportional Memory Status

`pmemstat` shows detailed **proportional** memory use of **Linux** processes by digesting:
* `/proc/{PID}/smaps`
* `/proc/{PID}/smaps_rollup`

Computing proportional memory avoids overstating memory use as many programs do (e.g., `top`). Specifically, proportional memory splits the cost of common memory to the processes sharing it (rather than counting common memory multiple times). And, it does not include uninstantiated virtual memory. 

Without `-o`, `pmemstat` shows less details and is much faster and sometimes less accurate (e.g., it will not report classes of memory such as SysV shared memory which are often are not present anyhow). With `-o` providing full detail, digging out the numbers is slower; thus, `pmemstat -o` may take a few seconds to start, but, in its loop mode, refreshes are relatively fast and efficient by avoiding recomputing unchanged numbers. When in its window mode, typing `o` toggles low/high detail.

`pmemstat`'s grouping feature rolls up the resources of multiple processes of a feature (e.g., a browser) to make the total memory/cpu impact much more apparent.

Its looping features allow monitoring for changes in memory growth which may be "leaks".  Segregating memory by types can make identifying leaks faster and more certain.

**`pmemstat` has these features**:
* In its **window mode**, `pmemstat` updates the terminal in place (using "curses") rather than scrolling.
* Showing **CPU use**, too, which makes `pmemstat` a viable alternative to `top` for regular use (although more specialized and still focused on accurate memory representation).
* Supports **inline search** (press `/` to search-as-you-type with live filtering).
* Supports **killing processes** with inline confirmation (press `y` to confirm, ESC to cancel).
* **Grouping by cgroup v2** (`-g cgroup`), so services, scopes and containers can be compared using both proportional PSS and the kernel's own `memory.current`/`memory.stat`/`memory.pressure`.
* **Memory-growth ("leak") annotations**: press `G` (or use `--growth-style`) to annotate the top-N growers inline (e.g. `13M 2h3m`) using a geometric, self-forgetting baseline; a system-wide growth/attribution line summarizes `ΔUsed = ΔTOTALS + Δ(Sh+Tmp) + ΔOthK + ΔOthU`.
* And several **new options** that can be **controlled dynamically** if in window mode.

## How `pmemstat` Compares to Other Tools
Reach for `top` or `ps` when you need a tool that is always installed, and `htop` when you want a friendly full-screen overview. Where `pmemstat` differs is its focus on **accurate, proportional memory**: instead of reporting each process's resident set size (RES/RSS) — which double-counts shared pages and ignores uninstantiated virtual memory — it digests `smaps`/`smaps_rollup` to attribute shared memory fairly and to break memory down by type. That makes `pmemstat` slower to start (especially with `-o`) but far more truthful about who is really using RAM. The table below compares it against its closest peers: `smem` (the other tool that derives **PSS** from `smaps`) and `systemd-cgtop` (whose per-cgroup view `pmemstat` now mirrors, but with proportional numbers).

| Feature | `pmemstat` | `smem` | `systemd-cgtop` | `htop` |
|---|---|---|---|---|
| **Proportional memory (PSS)** accounting | ✓ core design | ✓ core design | ✗ (`memory.current`) | ✗ (RES, double-counts) |
| **Memory-type breakdown** (shared/swap/stack/text/data) | ✓ | ✗ (USS/PSS/RSS/Swap only) | ✗ | ✗ |
| **Proportional swap** per grouping | ✓ | ✗ (flat Swap column) | ✗ | ✗ (total swap only) |
| **Group processes** by executable/command | ✓ aggregates PIDs into one row | ✓ by command by default | ✗ | ✗ (tree view only) |
| **Group by cgroup v2** (services, scopes, containers) | ✓ (`-g cgroup`) | ✗ | ✓ (its core design) | ✗ |
| **Memory-leak / growth monitoring** (annotates top-N growers; only re-shows significant growth) | ✓ (`L`, `-k`, growth sort) | ✗ | ✗ | ✗ |
| **zRAM-aware effective RAM** (eTot/eUsed/eAvail) | ✓ | ✗ | ✗ | ✗ |
| **System pressure (PSI)** in header (memory/cpu/io, some/full) | ✓ (`-P`) | ✗ | ✗ | ✗ |
| **Data source** | `/proc/{PID}/smaps*` | `/proc/{PID}/smaps` | `/sys/fs/cgroup` | `/proc` + libs |
| **Interactive full-screen window** | ✓ (`console-window`/curses) | ✗ (one-shot) | ✓ | ✓ |
| **CPU usage reporting** | ✓ | ✗ (memory only) | ✓ | ✓ |
| **Kill processes** | ✓ (inline confirm) | ✗ | ✗ | ✓ |
| **Inline search-as-you-type** | ✓ (`/`) | ✗ | ✗ | ✓ (F3) |
| **Requires root for all-process detail** | ✓ (else only your own) | ✓ (else only your own) | ✗ | ✗ |

In short: reach for `top`/`htop` when you want a fast, broad system overview, and reach for `pmemstat` when you need to know **how much memory a program (or group of programs) is truly responsible for**, to break that memory down by type or by cgroup, or to watch for creeping growth that suggests a leak. For **per-mapped-file attribution** (which shared library or mmap'd file costs the most RAM), use `smem -m` or `pmap`; `pmemstat` deliberately stays focused on per-process and per-cgroup responsibility rather than rolling PSS up by mapped file.

## Installation Options
Note that:
* `pmemstat` requires **Python 3.10 or later**.
* `pmemstat` needs root privileges to read the memory statistics (`smaps`/`smaps_rollup`) for **all** processes; without them it reports only your own processes.
* By default `pmemstat` runs as the invoking user and shows a hint; it does **not** silently escalate to root.
* Where it is installed matters: a system-wide install (e.g. `sudo pipx install --global pmemstat`) is visible to both your user and root, so `sudo pmemstat` works; a per-user install lives in `~/.local/bin`, which root does not search, so you either run it as your user or let it elevate itself.
* To have it re-run itself under `sudo` automatically, opt in once with `export PMEMSTAT_ARGS=--sudo` in your shell profile, or pass `--sudo` per invocation (for a system-wide install you can also simply run `sudo pmemstat`).
* For a persistent set of default flags, export `PMEMSTAT_ARGS` with a shell-quoted argument string, e.g. `export PMEMSTAT_ARGS='--sudo --psi --loop 3 -s name'`. It is tokenized like a command line and prepended to the real arguments, so anything typed on the command line still takes precedence.
* When `--sudo` is requested but elevation cannot happen (no terminal for a `sudo` prompt and sudo not pre-authorized, or the install is not importable by the isolated interpreter), `pmemstat` fails with a clear message and a non-zero exit instead of silently downgrading to a user-only view. It never hangs waiting for a password.
* To force user-only operation and disable auto-elevation, use the `--run-as-user` or `-U` option.
* `pmemstat` depends on `console-window`, pinned to an **exact version** on purpose. That package provides the curses UI and is deliberately not allowed to float: the pin protects against unexpected upstream changes and preserves the author's freedom to make backwards-incompatible UI revisions. Installing `pmemstat` pulls the pinned version automatically; do not "upgrade" `console-window` independently.

See the Quick Start at the top for preferred install instructions using `pipx`. If not acceptable, see the "Alternative Installation Options" section below.


## Usage
```
usage: pmemstat [-h] [-D] [-C] [-P] [-g {exe,cmd,pid,cgroup,cgroupCharge}]
        [-f] [-k MIN_DELTA_KB] [-l LOOP_SECS] [-L CMDLEN] [-t TOP_PCT]
        [-n] [-U] [--sudo] [--save-history-now] [-o] [-u {MB,mB,KB,human}]
        [-s {mem,cpu,name,growth}] [--growth-style {off,both,growth,rate}]
        [--growth-top {3,10,30,all}] [-/ SEARCH] [-W] [pids ...]

positional arguments:
  pids                  list of pids/groups (none means every accessible pid)

options:
  -h, --help            show this help message and exit
  -D, --debug           debug mode (the more Ds, the higher the debug level)
  -C, --no-cpu          do NOT report percent CPU (only in window mode)
  -P, --psi             show PSI (system pressure in header + memPSI% column)
                        [dflt=off]
  -g {exe,cmd,pid,cgroup}, --groupby {exe,cmd,pid,cgroup}
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
  --sudo                re-run self as root via sudo (or set
                        PMEMSTAT_ARGS=--sudo)
  --save-history-now    save current stats to the history ledger and exit
                        (implies --sudo; ignores other options/environment)
  -o, --others          expand "other" into shSYSV, shOth, stack, text
  -u {MB,mB,KB,human}, --units {MB,mB,KB,human}
                        units of memory [dflt=MB]
  -s {mem,cpu,name,growth}, --sortby {mem,cpu,name,growth}
                        sort method for presenting rows
  --growth-style {off,both,growth,rate}
                        leak/growth style: off|both|growth|rate [dflt=off]
  --growth-top {3,10,30,all}
                        annotate only the top-N growers [dflt=3]
  -/ SEARCH, --search SEARCH
                        show items with search string in name
  -W, --no-window       show in "curses" window [disables: -D,-t,-L]

```
Explanation of some options and arguments:
* `-g {exe,cmd,pid,cgroup,cgroupCharge}, --groupby {...}` -  select the grouping of memory stats for reporting.
    * `exe` - group by basename of the executable (the default)
    * `cmd` - group by the truncated command line (use `-L CMDLEN` to choose length)
    * `pid` - group by one process
    * `cgroup` - group by cgroup v2 path (services, scopes and containers), showing the derived **footprint** total; see "Grouping by cgroup v2" below
    * `cgroupCharge` - the same cgroup v2 grouping, but each row's slices and total show the kernel's `memory.current` charge instead of the derived footprint; see "Grouping by cgroup v2" below
* `-P, --psi` - show PSI (off by default): adds a **system pressure (PSI)** table to the header *and* the per-cgroup `memPSI%` column; see "System pressure (PSI)" and "Grouping by cgroup v2" below. In window mode this can also be toggled with the `p` key.
* `-k MIN_DELTA_KB, --min-delta-kb MIN_DELTA_KB` - when looping, how much change in memory use is required to show the grouping in subsequent loops; note:
    * a positive `MIN_DELTA_KB` means the total memory of the groupin must **grow** by that amount (in KB)
    * a non-positive `MIN_DELTA_KB` means the total memory of the grouping must **change** by that amount (in KB)
    * it also gates the growth annotation: a row is annotated only if its absolute growth (in KB) is at least `-k`
* `--growth-style {off,both,growth,rate}` - the inline growth ("leak") annotation for the groups that are growing (default `off`); in window mode the `G` key cycles it. The baseline is geometric and self-forgetting, so startup bursts age out. See "Memory growth (leak) detection" below.
* `--growth-top {3,10,30,all}` - annotate only the top-N growers by the displayed metric (default `3`; `all` = no cap); in window mode the `t` key cycles it
* `pids` - the positional arguments may be pids (i.e., numbers) or the names of executables (as shown by `-gexe`) 


## Grouping by cgroup v2
With `-g cgroup` (or cycling `g` in window mode) `pmemstat` groups processes by their **cgroup v2** path (read from `/proc/<pid>/cgroup`), which corresponds to systemd services/scopes and to container sandboxes. Each row is labelled with the cgroup's leaf unit name (e.g. `foo.service`); systemd's `\xNN` escapes are decoded for readability, and a trailing `+` marks a cgroup that also contains descendant cgroups listed on their own rows. If the leaf name matches none of the row's member executables — common with launcher-created app scopes (e.g. niri's `app-niri-fuzzel-*.scope`, which actually host the launched app such as VS Code or Vivaldi) — the row is labelled with the dominant member executable instead; the full cgroup path stays searchable with `/`.

Because `pmemstat` computes **proportional** memory (PSS), the `ptotal` column is *not* the same number that `systemd-cgtop`, `docker stats` or `podman stats` report: those use the kernel's `memory.current`, which is not proportional and over-counts pages shared between processes. For that reason the cgroup groupings keep `ptotal` as a left-hand **reference** column (the tool's namesake proportional total) but do **not** use it as the row total; the row total is the kernel charge, and the grouping chooses which charge: `-g cgroup` shows the derived `footprt`, `-g cgroupCharge` shows `memory.current` (`kcharge`).

The kernel columns, read from `/sys/fs/cgroup/<path>/` in the same units as every other column (`-u`), are a decomposition of that charge. The slices to the left of the total add up to it exactly:
* `anon` - `memory.stat`'s `anon`: anonymous memory charged to the cgroup
* `cache` - page cache: `file` (in the `kcharge` view) or `file - inactive_file` (in the `footprt` view, where reclaimable inactive cache is removed)
* `kmem` - kernel memory: `kernel` (`kcharge`) or `kernel - slab_reclaimable` (`footprt`)
* `swap` - the kernel swap charge (`footprt` view only; swap is not part of `memory.current`). Distinct from `pswap` (the smaps-proportional swap shown in other modes)
* `oK` - the remainder, `total - (anon + cache + kmem [+ swap])`; normally just `sock`, it guarantees the row adds up
* `footprt` / `kcharge` - the row **total** for the active grouping (`cgroup` -> `footprt`; `cgroupCharge` -> `kcharge`):
    * `footprt` (default) - a derived **footprint**: `memory.current - inactive_file - slab_reclaimable + swap`, a stable "what this cgroup really holds" number; also the growth metric
    * `kcharge` - `memory.current`, the kernel's own charge (the verifiable number behind `systemd-cgtop`/`docker stats`, and what `memory.max`/the OOM killer act on)
* `memPSI%` - `memory.pressure`'s `some avg10`: memory-pressure stall time as a percentage; shown only with `-P` (PSI), beside `cpu%`

So the memory columns of a `-g cgroup` row read `ptotal | anon cache kmem [swap] oK | <total>`: the leftmost value is pmemstat's proportional total (for comparison), and the bracketed slice block sums to the view total on the right. The raw `memory.current` is visible in the `kcharge` view (and is used internally by `--growth-style`/`Oth*` attribution).

These columns appear only in the cgroup groupings (`-g cgroup` / `-g cgroupCharge`), and are omitted entirely when the kernel exposes no cgroup v2 data (e.g. a cgroup v1 host).

cgroup v2 accounting is **hierarchical**: a cgroup's `anon`/`cache`/`kmem`/`swap`/`footprt`/`kcharge` already include its descendant cgroups. A row whose cgroup also contains descendant cgroups listed separately is flagged with a trailing `+`, and **those descendants' figures are subtracted** so the row shows only that cgroup's own share. Consequently every row is its own share and the rows add up to the `TOTALS` row, and within each row the slice block adds up to the view total. (Because `footprt` is *derived* (`memory.current - inactive_file - slab_reclaimable + swap`), a `footprt`-view row does not equal the raw `cat /sys/fs/cgroup/.../memory.current`; switch to the `kcharge` view to see that raw value.) The unified root (`/`, labelled `(root)`) is the ancestor of every cgroup, so if a process lives directly in it — as can happen inside a container, where it may be the only cgroup visible — that `(root)+` row counts every other row as its descendant. `memPSI%` is not totalled — it shows `n/a` in `TOTALS` and in the `---- OTHERS ----` row, since a sum of pressure percentages is meaningless.

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
20:49:12 Tot=7.6G Used=6.2G Avail=1.4G OthK=512.0M OthU=3.6G Sh+Tmp=477.7M PIDs=174
     2.4%/ker MajF/s=2  zRAM=813.2M CR=4.3 eTot:16.8G eUsed:8.8G eAvail:8.0G
 cpu%      pswap   other    data  ptotal   key/info (exe by mem)
    60.8   2,535     593   3,988   7,116 T 174x --TOTALS in MB --
───────────────────────────────────────────────────────────────────────────────
     5.9   1,366      90   2,110   3,567   24x browser
    16.6      89     117     822   1,028   9x code
     5.9     270      32     291     593 3.5G 2h3m 1x firefox
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
* an optional **PSI block** (only with `-P`) as described in "System pressure (PSI)" above
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
        * **{growth} {interval}** - inline growth annotation for the top-N growers (e.g. `3.5G 2h3m`), shown only when the growth mode is not `off` (`G` in window mode)
    * **key/info** which is a quantifier plus the grouping key. The quantifier may be:
        * **{PID}** - when the grouping line represents one process (for option `-gexe`).
        * **{num}x** - where {num} is the number of processes in the grouping.
        
## Memory growth (leak) detection

With `--growth-style` (or the `G` key in window mode) `pmemstat` annotates the
groups that are growing, making slow leaks visible without scrolling history:

```
     5.9     270      32     291     593 3.5G 2h3m 1x firefox
```

* The annotation is `{human growth} {interval}` (e.g. `3.5G 2h3m`); the `rate`
  mode shows a per-day projection (`/d`, e.g. `3.5G/d`) once the baseline
  interval is at least a minute, and `growth` shows just the size.
* It is measured against a **geometric, self-forgetting baseline** (anchors at
  16, 64, 256, ... seconds): a process's first 16 seconds never count, so
  startup bursts age out, and growth is never negative (a reduction is zero).
* The baseline is kept **per grouping, from the moment the tool starts**, so
  changing the grouping (`g` in window mode) does not reset the leak history:
  every grouping is tracked in parallel. The cost is one extra pass over the
  already-read per-process rollups for the non-cgroup views, plus one read per
  distinct cgroup (shared by both cgroup views, and reused when a cgroup view
  is the active one).
* A row is annotated only if its absolute growth (KB) is at least `-k` **and**
  it is among the top-N growers (`--growth-top`, default 3; `t` cycles
  3/10/30/all). The `G` key cycles `off -> both -> growth -> rate -> off`.
  (In window mode every row is listed, so annotations are always visible; in
  non-window loop mode a row is re-shown only when it changes enough for `-k`.)
* `-s growth` sorts rows by the displayed growth metric (rate in `both`/`rate`).
* With a cgroup grouping the metric is that grouping's total (`footprt` for
  `-g cgroup`, or `kcharge` for `-g cgroupCharge`; see "Grouping by cgroup v2")
  rather than proportional PSS.
* A system-wide line is shown beneath the leader (same mode) and closes the
  accounting identity `ΔUsed = ΔTOTALS(ptotal) + Δ(Sh+Tmp) + ΔOthK + ΔOthU`,
  where `OthK = SUnreclaim + KernelStack + PageTables` and `OthU` is the
  remainder, so kernel/driver growth (`ΔOthK`/`ΔOthU`) is separable from
  userspace (`ΔTOTALS`) and tmpfs (`Δ(Sh+Tmp)`).
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

## Key Legend (Window Mode)
The top line of the header is an always-visible key legend (dimmed, left-aligned
so it sits under the leader's `Tot=` value) with the live search field appended
in bold, so the available keys are in evidence without opening the help screen:

    [?]help [g]roup [u]nits [s]ort [c]pu [K]ill [p]SI [G]rowth [t]op /{regex}

`?` remains the gateway to the complete list of keys plus the navigation keys.
Because `?` is listed first, a narrow terminal truncates only the least-critical
trailing entries, never the way to the full help screen.

## Help Screen (in Window Mode, Press '?')
In window mode, press '?' to enter the help screen which looks like:

![helpscreen example](https://github.com/joedefen/pmemstat/blob/main/images/help-screen.png?raw=true)

**Notes:**
* There are a number of navigation keys (mostly following vim conventions); in the help screen, they apply to help screen; otherwise, they apply to main screen.
* Below the line, there are a number of keys/options; when you type an option key (e.g, "c"), it will highlight the next option value (e.g., "off"); when you change options, they will be applied to the next loop of the main menu.
    * These option keys can be used in the main menu (e.g., pressing "c" will change hide or reveal the CPU column w/o entering the help screen).
    
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