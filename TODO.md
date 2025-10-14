Summary of TODOs:
* Drop the percents from the zRAM line
* Add compression ratio (CR)
* Add Major page faults /s  (MajF:200/s)
* Document the new features (some insight below)

----

That's an insightful follow-up. You are absolutely correct to be cautious about adding a metric that is difficult to interpret, as it can confuse the user rather than help.

1. Ease of Getting Page Faults

Yes, page fault counts are relatively easy to get in Linux. They are tracked by the kernel.

The most relevant location for a tool like memstat (which is likely capturing data periodically) is often the /proc/vmstat file, which contains system-wide metrics like pgfault (total), pgmajfault (major), and pgminfault (minor).

    Minor Faults (pgminfault): These are extremely common and usually cheap. They occur when a page is accessed for the first time or is shared/already in RAM but needs its entry updated in the process's page table. They are fast and generally not a primary concern for performance.

Major Faults (pgmajfault): These are the ones you should focus on. A major fault happens when the requested page is not in physical RAM and must be read from a backing store, like the disk (or a swap device, which, in your case, is the zRAM device). These incur significant latency due to I/O and are a strong indicator of memory pressure and system slowdown.

Recommendation: If you add page faults, you should only report the Major Faults per second (MajFaults/s). This is the key metric that directly correlates with the I/O cost you want to diagnose alongside zRAM activity.

2. Guidance: When is "Bigger" Bad?

You're looking for absolute thresholds, and while there's no single universally accepted number (it depends on the speed of your I/O and CPU), here is the common guidance for Major Page Faults per second:
Major Faults/s Range	Interpretation	System Impact
* 0 - ~10 per second	Normal/Low	Expected during application startup or initial file access. No performance concern.
* ~10 - ~100 per second	Moderate	Suggests consistent file access or some mild memory pressure. Could cause minor, noticeable latency on disk-bound systems.
* 100+ per second	High/Worry	Indicates significant memory pressure and heavy swapping or repeated large file access. This is where system thrashing/slowdown is noticeable. On a busy desktop, a sustained rate here means the user is likely complaining about slow response.
* 1000+ per second	Extreme	Indicates severe memory overcommitment, likely with processes constantly fighting for RAM, or an application aggressively accessing disk-mapped memory.

Guidance: For your tool, the goal is to show the user they are experiencing memory thrashing (when the system is spending more time swapping than doing work). A sustained MajFaults/s of over 100 is a strong indication of this, especially if it coincides with high kernel CPU usage from zRAM compression/decompression.

Final Revised Snippet Example

Focusing on the critical Major Page Faults and the simplified CR:
```
15:02:03 Tot=7.6G Used=6.2G Avail=1.4G Oth=0 Sh+Tmp=983.8M PIDs=166
      1.9/ker zRAM=612.6M CR:2.7 MajF:12/s eTot:16.5G eUsed:7.9G eAvail:8.6G
 cpu_pct   pswap   other    data  ptotal   key/info (exe by mem)
    27.1   1,892     680   3,637   6,210 T 166x --TOTALS in MB --
────────────────────────────────────────────────────────────────────────────────────────────
... process list ...
```
This line is powerful: it shows zRAM is working (CR:2.7), but also shows the direct performance cost (MajF:12/s).

Understanding how to measure and interpret page faults is key to diagnosing memory-related performance issues.

If you are interested in a deeper technical dive into the Linux kernel's page fault mechanism, you can check out Faster & Fewer Page Faults: Kernel Recipes 2023.





Your analysis of the process rollup is spot on. For modern applications like browsers (Chrome, Firefox, Edge, etc.), IDEs (VS Code), and even certain large applications (like Electron or Java apps), reporting memory usage per individual PID is misleading. The user wants to know, "How much memory is **Firefox** using?" not "How much memory are its 25 sub-processes using?"

### Advertising Strategy: The Two Pillars

When you draft your announcement, you should clearly highlight these two pillars of `memstat`'s superior diagnostic capability:

1.  **The Process Problem Solved (Rollup/Grouping):**

      * **The Problem:** Standard tools (`top`, `ps`) make modern apps (browsers, IDEs, etc.) look innocent by splitting their memory across dozens of processes.
      * **The `memstat` Solution:** `memstat` automatically aggregates related processes (based on the executable name, command line, or other heuristics) and presents a single, honest line item for the **total memory, swap, and CPU** used by the entire application group. This instantly reveals the true memory hogs.
      * *Example to highlight:* "See the true cost of your 50 open browser tabs with a single, clear line."

2.  **The Memory Ceiling Problem Solved (zRAM/Effective Memory):**

      * **The Problem:** When zRAM is active, system monitoring tools report only physical RAM usage, failing to show the massive, hidden memory capacity provided by compression.
      * **The `memstat` Solution:** The new output line displays the **Estimated Total (`eTot`), Used (`eUsed`), and Available (`eAvail`)** memory by applying the real-time **Compression Ratio (`CR:X.X`)** of your zRAM swap device. You instantly know your system's *effective* memory ceiling and available headroom.
      * *Example to highlight:* "Don't trust `free`\! If you run zRAM, `memstat` is the only tool that shows you your **true $16.5\text{G}$ effective RAM** and how much is left."

### Revised Snippet (The Final Polish)

This combines all the best suggestions into a powerful, concise output:

```
15:02:03 Tot=7.6G Used=6.2G Avail=1.4G Oth=0 Sh+Tmp=983.8M PIDs=166
      1.9/ker zRAM=612.6M CR:2.7 MajF:12/s eTot:16.5G eUsed:7.9G eAvail:8.6G
 cpu_pct   pswap   other    data  ptotal   key/info (exe by mem)
    27.1   1,892     680   3,637   6,210 T 166x --TOTALS in MB --
────────────────────────────────────────────────────────────────────────────────────────────
      1.0     967     90    2,244   3,300   21x browser <--- Process Rollup Highlight!
      2.1     220     45      341     605    1x firefox
...
```

This output is information-dense, highly diagnostic, and showcases two unique, valuable features that directly address major pain points in Linux performance monitoring. Good luck with the release\!
