// A test program for 'pmemstat' which grows its memory over time including:
//    - stack
//    - heap
//    - SysV shared memory
//    - memory mapped files
// Several instances of the this programs can be run, and, if so, the
// programs will share the SysV shared memory and the memory mapped files.
//
// AGGRESSIVENESS vs. SAFETY
//   Every loop grows three tracked pools by STEP (1 MiB by default) each --
//   the SysV shared memory, a memory-mapped file, and the heap -- plus a
//   smaller amount of stack.  That is ~3 MiB of new memory per loop, well
//   above pmemstat's default -k gate (1000 KB), so the program reliably shows
//   up as a grower (for example with --growth-top all) instead of being
//   filtered out as "unchanged" after the first loop.
//
//   The growth is deliberately bounded so a run cannot push the system toward
//   the OOM killer:
//     * the total is capped to a budget derived from physical RAM (at most
//       ~6% of RAM, itself capped by a hard ceiling), and
//     * the recursion depth -- which is what grows the stack -- is limited so
//       that depth * STACK_STEP stays well under RLIMIT_STACK; a stack
//       overflow would SIGSEGV, which is worse than growing too little.
//   Individual allocations are non-fatal: if one fails, that pool simply stops
//   growing and the run continues.
#include <stdio.h>
#include <stdlib.h>
#include <stdbool.h>
#include <string.h>
#include <sys/types.h>
#include <sys/ipc.h>
#include <sys/shm.h>
#include <sys/sem.h>
#include <unistd.h>
#include <sys/stat.h>
#include <fcntl.h>
#include <sys/mman.h>
#include <sys/resource.h>
#include <errno.h>

#define MAX_STEPS 128                      /* max chunks per pool (array bound) */
#define STEP_SIZE ((size_t)(1024*1024))    /* default bytes added per pool/loop */
#define STACK_STEP ((size_t)(32*1024))     /* bytes of stack per recursion frame */
#define HARD_BUDGET ((size_t)(256*1024*1024)) /* ceiling on total memory used */
#define MIN_BUDGET  ((size_t)(16*1024*1024))  /* never cap below this */
#define BASE_KEY 0xffeeddcc
#define DIM(A) (sizeof(A)/sizeof(A[0]))
#define FILENAME "/tmp/memmapfile"

int rm_shmem();
int rm_mmap();

struct Args {
    int sleep_sec;  // delay per loop
    int n_loops;    // requested number of loops
    size_t budget;  // total bytes to spread across all pools (0 => derive)
} Args = {.sleep_sec = 10, .n_loops = MAX_STEPS, .budget = 0};

struct MyData {
    int n_shmem; void *shmemp[MAX_STEPS];
    int n_malloc; void *mallocp[MAX_STEPS];
    int n_mmap; int mm_fd; unsigned char *mm_ptr;
} MyData;

int LoopCnt = 0;

size_t g_step = STEP_SIZE;  // bytes added to a pool per loop (tunable with -c)
int g_plan = MAX_STEPS;     // effective number of loops actually run

// Keeps a recursion frame's stack buffer from being optimized away.
volatile unsigned char *g_stack_sink;

int incr_sem() {
    // returns the count of these processes (so the first can initialize)
    int semid, val0;
    int key = BASE_KEY;
    if ((semid = semget(key, 2, 0644 | IPC_CREAT)) == -1) {
        perror("shmget");
        exit(1);
    }
    struct sembuf sembufs[2] = {
          { .sem_num = 0, .sem_op = 1, .sem_flg = SEM_UNDO}
        , { .sem_num = 1, .sem_op = 1, .sem_flg = 0}
    };
    semop(semid, &sembufs[0], 1);
    val0 = semctl(semid, 0, GETVAL);
    printf("semval=%d\n", val0);

    for (bool done = false; !done; ) {
        if (val0 == 1) {
            rm_shmem();
            rm_mmap();
            semop(semid, &sembufs[1], 1);
            done = true;
        } else {
            for (int loop = 0, val1 = 0; val1 != 1; loop++) {
                val1 = semctl(semid, 1, GETVAL);
                if (val1 >= 0) {
                    done = true;
                    break;
                } else if (loop >= val0) {
                    printf("%d gave up on init; forcing it, loop=%d", val0, loop);
                    val0 = 1;
                    break;
                } else {
                    sleep(1);
                    printf("%d waiting on val1==0, got %d, loop %d\n", val0, val1, loop);
                }
            }
        }
    }
    return val0;
}

////////////////////////////// PLANNING / SAFETY

static size_t compute_budget(void) {
    // Keep well away from the OOM killer: at most ~6% of physical RAM,
    // but never target more than HARD_BUDGET (nor less than MIN_BUDGET).
    size_t budget = HARD_BUDGET;
    long pages = sysconf(_SC_PHYS_PAGES);
    long psize = sysconf(_SC_PAGESIZE);
    if (pages > 0 && psize > 0) {
        size_t ram = (size_t)pages * (size_t)psize;
        size_t ram_budget = ram / 16;  // ~6% of RAM
        if (ram_budget < budget)
            budget = ram_budget;
    }
    if (budget < MIN_BUDGET)
        budget = MIN_BUDGET;
    return budget;
}

static int stack_depth_limit(void) {
    // The stack is grown by recursion, so the depth must leave generous
    // headroom below RLIMIT_STACK (overflow => SIGSEGV).
    struct rlimit rl;
    if (getrlimit(RLIMIT_STACK, &rl) != 0 || rl.rlim_cur == RLIM_INFINITY)
        return MAX_STEPS;
    size_t usable = (size_t)rl.rlim_cur / 2;  // leave half for the runtime
    int depth = (int)(usable / STACK_STEP);
    if (depth < 1)
        depth = 1;
    if (depth > MAX_STEPS)
        depth = MAX_STEPS;
    return depth;
}

static int plan_loops(void) {
    // Effective loop count = requested, bounded by the array size, by the
    // stack rlimit, and by the memory budget.
    int loops = Args.n_loops;
    if (loops > MAX_STEPS)
        loops = MAX_STEPS;
    if (loops < 1)
        loops = 1;
    int depth = stack_depth_limit();
    if (loops > depth)
        loops = depth;
    size_t per_loop = 3 * g_step;  // shmem + mmap + heap; stack is rlimit-bound
    if (per_loop > 0) {
        size_t by_budget = Args.budget / per_loop;
        if (by_budget < (size_t)loops)
            loops = (int)by_budget;
    }
    if (loops < 1)
        loops = 1;
    return loops;
}

////////////////////////////// SHMEM

int add_shmem() {
    int idx = MyData.n_shmem;
    if (idx >= g_plan) {
        return 0;
    }
    key_t key = BASE_KEY + idx;
    int shmid;
    char *data;
    /*  create/attach the segment: */
    if ((shmid = shmget(key, g_step, 0644 | IPC_CREAT)) == -1) {
        if (errno == EINVAL) {
            // A stale segment (e.g. from an earlier run with a smaller step)
            // blocks creation: remove it and retry once.
            int old = shmget(key, 0, 0);  // size is ignored for a bare lookup
            if (old != -1) {
                shmctl(old, IPC_RMID, NULL);
                shmid = shmget(key, g_step, 0644 | IPC_CREAT);
            }
        }
        if (shmid == -1) {
            perror("shmget");
            return 0;  // non-fatal: stop growing shared memory
        }
    }
    /* attach to the segment to get a pointer to it: */
    data = shmat(shmid, NULL, 0);
    if (data == (char *)(-1)) {
        perror("shmat");
        return 0;  // non-fatal
    }
    MyData.shmemp[idx] = data;
    MyData.n_shmem = idx + 1;
    memset(data, 'a', g_step);
    return 1;
}

int rm_shmem() {
    int shmid;
    int removed = 0;
    for (int idx = 0; idx < MAX_STEPS; idx++) {
        key_t key = BASE_KEY + idx;
        // Look the segment up by key alone (size is ignored on a bare lookup),
        // so a stale segment of any size is found and removed.  Fall back to
        // the creating size in case this kernel rejects a 0 size on lookup.
        if ((shmid = shmget(key, 0, 0)) == -1)
            if ((shmid = shmget(key, g_step, 0)) == -1)
                continue;
        if (shmctl(shmid, IPC_RMID, NULL) >= 0)
            removed += 1;
    }
    printf("removed %d segments\n", removed);
    return removed;
}


////////////////////////////// MMAP

int add_mmap() {
    int fd;
    unsigned char *ptr;
    if ((fd = MyData.mm_fd) < 0) {
        fd = open(FILENAME, O_RDWR|O_CREAT, 0666);
        if (fd < 0) {
            perror("open(/tmp/...)");
            return 0;  // non-fatal
        }
        MyData.mm_fd = fd;
        off_t span = (off_t)g_plan * (off_t)g_step;
        int rv = posix_fallocate(fd, 0, span);
        if (rv != 0) {  // posix_fallocate() returns an errno, not -1
            perror("posix_fallocate()");
            return 0;  // non-fatal
        }
    }
    if ((ptr = MyData.mm_ptr) == NULL) {
        size_t maplen = (size_t)g_plan * g_step;
        ptr = mmap(NULL, maplen, PROT_READ|PROT_WRITE, MAP_SHARED, fd, 0);
        if (ptr == MAP_FAILED) {
            perror("mmap()");
            return 0;  // non-fatal
        }
        MyData.mm_ptr = ptr;
    }
    if (MyData.n_mmap >= g_plan) {
        return 0;
    }
    unsigned char* bottom = &MyData.mm_ptr[(size_t)MyData.n_mmap * g_step];
    memset(bottom, 'm', g_step);
    MyData.n_mmap += 1;
    return 1;
}

int rm_mmap() {
    unlink(FILENAME);
    return 0;
}

////////////////////////////// MALLOC

int add_malloc() {
    if (MyData.n_malloc >= g_plan) {
        return 0;
    }
    void *p = malloc(g_step);
    if (!p) {
        perror("malloc");
        return 0;  // non-fatal: stop growing the heap
    }
    memset(p, 'h', g_step);
    MyData.mallocp[MyData.n_malloc] = p;
    MyData.n_malloc += 1;
    return 1;
}

////////////////////////////// MAIN LOOP STUFF

int loop() {
    // Grow the stack by STACK_STEP: this frame stays live until the recursion
    // unwinds, so each nested call adds another STACK_STEP of stack.  The sink
    // keeps the buffer from being optimized away at higher -O levels.
    char stack[STACK_STEP];
    memset(stack, 'h', STACK_STEP);
    stack[0] = (char)LoopCnt;
    g_stack_sink = (unsigned char *)stack;

    add_shmem();
    add_mmap();
    add_malloc();
    printf("%d: loop=%d, shmem=%luK mmap=%luK stack=%luK heap=%luK\n",
            getpid(),
            LoopCnt+1,
            (unsigned long)(MyData.n_shmem * g_step / 1024),
            (unsigned long)(MyData.n_mmap * g_step / 1024),
            (unsigned long)((size_t)(LoopCnt + 1) * STACK_STEP / 1024),
            (unsigned long)(MyData.n_malloc * g_step / 1024));
    sleep(Args.sleep_sec);
    if (++LoopCnt >= g_plan) {
        exit(0);
    }
    loop(); // NOTE: recurses (to be able to add stack)
    return 0;
}


int main(int argc, char *argv[])
{
    int opt;
    while ((opt = getopt(argc, argv, "qsn:c:b:")) != -1) {
        switch(opt) {
            case 'q': Args.sleep_sec = 1; break;
            case 's': Args.n_loops = 16; break;
            case 'n': Args.n_loops = atoi(optarg); break;
            case 'c': g_step = (size_t)atol(optarg) * 1024; break;
            case 'b': Args.budget = (size_t)atol(optarg) * 1024 * 1024; break;
            default:
                printf("USE: %s {-q|-s|-n loops|-c KiB/loop|-b MiB budget}\n",
                       argv[0]);
                exit(1);
        }
    }
    if (g_step == 0) {
        g_step = STEP_SIZE;
    }
    if (Args.budget == 0) {
        Args.budget = compute_budget();
    }
    g_plan = plan_loops();

    memset(&MyData, 0, sizeof(MyData));
    MyData.mm_fd = -1;

    printf("memtest: step=%luK/loop, loops=%d, budget=%luM, sleep=%ds\n",
           (unsigned long)(g_step / 1024),
           g_plan,
           (unsigned long)(Args.budget / (1024*1024)),
           Args.sleep_sec);

    incr_sem();
    loop();

    return 0;
}
