# pmemstat - Proportional Memory Status

pmemstat is a tool to show the detailed memory use of Linux process by digesting:
* /proc/{pid}/smaps_rollup
* /proc/{pid}/smaps

## Usage
```
usage: pmemstat [-h] [-D] [-g {exe,cmd,pid}] [-k MIN_DELTA_KB] [-l LOOP_SECS]
                [-L CMDLEN] [-t TOP_PCT] [-u {MB,mB,KB}]
                [pids [pids ...]]

positional arguments:
  pids                  list of pids/groups (none means all we can read)

optional arguments:
  -h, --help            show this help message and exit
  -D, --debug           debug mode (the more Ds, the higher the debug level)
  -g {exe,cmd,pid}, --groupby {exe,cmd,pid}
                        grouping method for presenting rows
  -k MIN_DELTA_KB, --min-delta-kb MIN_DELTA_KB
                        minimum percent KB to show again [dflt=100 if DB else
                        1000
  -l LOOP_SECS, --loop LOOP_SECS
                        loop interval in seconds [dflt=0]
  -L CMDLEN, --cmdlen CMDLEN
                        max command line length for reporting/grouping
                        [dflt=36]
  -t TOP_PCT, --top-pct TOP_PCT
                        report group contributing to top pct of ptotal
                        [dflt=100]
  -u {MB,mB,KB}, --units {MB,mB,KB}
                        units of memory [dflt=MB]
```
Explanation of some options and arguments:
* `-g {exe,cmd,pid}, --groupby {exe,cmd,pid}` -  select how to group the memory stats for reporting.
    * `exe` - group by basename of the executable (the default)
    * `cmd` - group by the truncated command line (use `-L CMDLEN` to choose length)
    * `pid` - group by one process
* `-k MIN_DELTA_KB, --min-delta-kb MIN_DELTA_KB` - when looping, how much change in memory use is required to show the group in subsequent loops; note:
    * a positive `MIN_DELTA_KB` means the total memory of the group must **grow** by that amount (in KB)
    * a non-positive `MIN_DELTA_KB` means the total memory of the group must **change** by that amount (in KB)
* `pids` - the positional arguments may be pids (i.e., numbers) or the names of executables (as shown by `-gexe`) 


# Example Usage with Explanation of Output
![pmemstat example](images/Screenshot-KDEApps-2021-12-07.png)

On every loop, we see
* a leader line with:
    * the current time
    * from /proc/meminfo in MB, MemTotal, MemAvailable, and Dirty
    * how many PIDs are contributing to the report vs the total number of PIDs excluding kernel threads; if `pmemstat` is run as root, then all  PIDs can be seen; otherwise, you will be restrict to those for which you have permissions.

**TBD**
