> **Quick Start**: from the CLI
> * **If `python3 -V` shows v3.11 or later, install using `pipx`**:
>   * `python3 -m pip install --user pipx # if pipx not installed`
>   * `python3 -m pipx ensurepath # if needed (restart terminal)`
>   * `pipx upgrade pmemstat || pipx install pmemstat # to install/upgrade`
> * **Else for python3.10 and lesser versions, install using `pip`**:
>   * `python3 -m pip install --user --upgrade pmemstat`
> * **To run**:
>   * `pmemstat # to run`
>   * Type "?" within pmemstat, show help screen.


# pmemstat - Proportional Memory Status

`pmemstat` shows detailed **proportional** memory use of **Linux** processes by digesting:
* `/proc/{PID}/smaps`
* `/proc/{PID}/smaps_rollup`

Computing proportional memory avoids overstating memory use as many programs do (e.g., `top`). Specifically, proportional memory splits the cost of common memory to the processes sharing it (rather than counting common memory multiple times). And, it does not include uninstantiated virtual memory. 

Without `-o`, `pmemstat` shows less details and is much faster and sometimes less accurate (e.g., it will not report classes of memory such as SysV shared memory which are often are not present anyhow). With `-o` providing full detail, digging out the numbers is slower; thus, `pmemstat -o` may take a few seconds to start, but, in its loop mode, refreshes are relatively fast and efficient by avoiding recomputing unchanged numbers. When in its window mode, typing `o` toggles low/high detail.

`pmemstat`'s grouping feature rolls up the resources of multiple processes of a feature (e.g., a browser) to make the total memory/cpu impact much more apparent.

Its looping features allow monitoring for changes in memory growth which may be "leaks".  Segregating memory by types can make identifying leaks faster and more certain.

**In version 2.0, `pmemstat` has many new features including**:
* In its **window mode**, `pmemstat` updates the terminal in place (using "curses") rather than scrolling.
* Showing **CPU use**, too, which makes `pmemstat` a viable alternative to `top` for regular use (although more specialized and still focused on accurate memory representation).
* Supports **killing processes** that are selected visually with confirmation they are really gone (or not).
* And several **new options** that can be **controlled dynamically** if in window mode.

## Installation Options
Note that:
* `pmemstat` needs to run as root to read the memory statistics for all processes (which is normally desired).
* By default, `pmemstat` reruns itself as with `sudo` (thus you need `sudo` privileges).
* To defeat re-running with `sudo`, use the `--run-as-user` or `-U` option.

See the Quick Start at the top for preferred install instructions using `pipx`. If not acceptable, see the "Alternative Installation Options" section below.


## Usage
```
usage: pmemstat [-h] [-D] [-C] [-g {exe,cmd,pid}] [-f] [-k MIN_DELTA_KB]
        [-l LOOP_SECS] [-L CMDLEN] [-t TOP_PCT] [-n] [-U] [-o]
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
                        minimum delta KB to show again [dflt=100 if DB else 1000
  -l LOOP_SECS, --loop LOOP_SECS
                        loop interval in secs [dflt=5 if -w else 0]
  -L CMDLEN, --cmdlen CMDLEN
                        max shown command length [dflt=36 if not -w]
  -t TOP_PCT, --top-pct TOP_PCT
                        report group contributing to top pct of ptotal [dflt=100]
  -n, --numbers         show line numbers in report
  -U, --run-as-user     run as user (NOT as root)
  -o, --others          collapse shSYSV, shOth, stack, text into "other"
  -u {MB,mB,KB,human}, --units {MB,mB,KB,human}
                        units of memory [dflt=MB]
  -R, --no-rise         do NOT raise change/adds to top (only in window mode)
  -s {mem,cpu,name}, --sortby {mem,cpu,name}
                        grouping method for presenting rows
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
10:38:17 Tot=31.3G Avail=21.8G Oth=2.4G Tmp=326.7M Dirty=1.2M PIDs: 202/202
 cpu_pct   pswap   other    data  ptotal   key/info (exe by mem)
    37.9       0   1,670   5,193   6,862 T 202x --TOTALS in MB --
──────────────────────────────────────────────────────────────────────────────
     2.1       0     452   1,830   2,282   41x chrome
    18.5       0       2     827     830   2x python->main.py
     0.1       0      87     470     557   8x code
     0.7       0      63     393     456   1x plasmashell
     0.7       0     106     281     389   7x Code
     0.2       0      52     176     227   2x app
     0.0       0      17     200     217   1x DiscoverNotifier
     0.0       0      80     114     194   1x app.asar
     0.0       0      94      76     170   3x signal-desktop
     0.0       0       4     160     165   2x python->lsp_server.py
     2.9       0      29     101     129   1x kwin_x11
     0.0       0      94       9     102   2x Signal
    12.8       0     590     555   1,145 O 131x ---- OTHERS ----

```

In the default refreshed window loop, we see
* a **leader line** with:
    * the current time
    * from `/proc/meminfo` in MemTotal (Tot), MemAvailable (Avail), Tmp (Shmem+TmpFS), and Dirty.
    * 'Oth' is the unaccounted for memory belonging to the kernel, reserve,
       drivers, imprecision, etc.; `Oth = Tot - Avail - Tmp - ptotal`.
       * Features such as BTRFS, ZFS, zRAM, unattached SysV Shared Memory, etc., can cause 'Oth' to be significant.
       * Determining the contributors can be difficult, but start with `sudo slabtop -sc` and feature specific tools (e.g., `zpool list`).
    * how many PIDs are contributing to the report vs the total number of PIDs excluding kernel threads
* a **header line with the reported fields** including:
    * **pswap** - proportional use of swap (per smaps_rollup)
    * **other** - partly sums these categories (shown by the key, `o`):
      * **shSYSV** - proportional use of System V shared memory (per smaps)
      * **shOth** - proportional use of other shared memory (per smaps)
      * **stack** - exclusive use of stack memory per smaps
      * **text** - proportional use of memory for text (i.e., the read-only binary code, per smaps)
    * **data** - exclusive use of memory for data (i.e., exclusively used of "heap" memory, per smaps)
    * **ptotal** - proportional use of memory of all categories (i.e, sum of all columns to the left except **pswap**, per smaps)
    * (sometimes) **pss** - proportional use of memory (per smaps_rollup)
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

![helpscreen example](https://github.com/joedefen/pmemstat/blob/main/images/help-screen_2023-05-15.png?raw=true)

**Notes:**
* There are a number of navigation keys (mostly following vim conventions); in the help screen, they apply to help screen; otherwise, they apply to main screen.
* Below the line, there are a number of keys/options; when you type an option key (e.g, "c"), it will highlight the next option value (e.g., "off"); when you change options, they will be applied to the next loop of the main menu.
    * These option keys can be used in the main menu (e.g., pressing "c" will change hide or reveal the CPU column w/o entering the help screen).
    
## Kill Mode (Window Mode)
Pressing "K" enter "Kill Mode" where you use the navigation keys to highlight a row, and then press ENTER to kill the process(es) represented by that row.

## Scroll Position (Window Mode)

Sometimes, the horizontal line between the header and scrollable region has a reverse video block (under the "351" in this case):
![scroll-pos example](https://github.com/joedefen/pmemstat/blob/main/images/scroll-pos_2023-09-05.png?raw=true)

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
* Or **from PyPi as root**. This makes `pememstat` available to all users
   with `/usr/local/bin` on `$PATH`. Note: `PIP_BREAK_SYSTEM_PACKAGES=1`
   may be required on some distros.
```
        PIP_BREAK_SYSTEM_PACKAGES=1 sudo python -m pip install pmemstat
        # to uninstall: sudo python -m pip uninstall pmemstat
```
* Or **from GitHub, scripted install**. The included `deploy` script installs
   a single-file `pmemstat` to `/usr/bin/pmemstat`;
   `deploy` installs/reinstalls `stickytape`, too.
   These commands install/update `pmemstat` w/o leftovers (except `stickytape`):
```
        # NOTE: requires "git" to be installed beforehand
        cd /tmp; rm -rf pmemstat;
        git clone https://github.com/joedefen/pmemstat.git;
        ./pmemstat/deploy; rm -rf pmemstat
        # to uninstall run: sudo rm /usr/bin/pmemstat
```
* Or **from GitHub, manual install**: If you clone `pmemstat` from GitHub, then you may run its `deploy` script OR run "`pip install .`" as root or not OR just directly run `pmemstat/main.py`.