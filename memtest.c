// A test program for 'pmemstat' which grows its memory over time including:
//    - stack
//    - heap (TODO)
//    - SysV shared memory
//    - memory mapped files (TODO)
// Several instances of the this programs can be run, and, if so, the
// programs will share the SysV shared memory and the memory mapped files.
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

#define CNT 128 /* how big can things get */
#define CHUNK_SIZE ((size_t)(10*1024))  /* make it a 10K shared memory segment */
#define BASE_KEY 0xffeeddcc
#define DIM(A) (sizeof(A)/sizeof(A[0]))
#define FILENAME "/tmp/memmapfile"

int rm_shmem();
int rm_mmap();

struct Args {
    int sleep_sec; // delay per loop
    int n_loops; // number of loops
} Args = {.sleep_sec = 10, .n_loops = CNT};

struct MyData {
    int n_shmem; void *shmemp[CNT];
    int n_malloc; void *mallocp[CNT];
    int n_mmap; int mm_fd; unsigned char *mm_ptr;
} MyData;

int LoopCnt = 0;

int incr_sem() {
    // returns the count of these processes (so the first can initialize)
    int semid, val0, val1;
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

    
//////////////////////////////// SHMEM

int add_shmem() {
    int idx = MyData.n_shmem;
    if (idx >= CNT) {
        printf("add_shmem: too many shmemp\n");
        exit(1);
    }
    key_t key = BASE_KEY + idx;
    int shmid;
    char *data;
    /*  create/attach the segment: */
    if ((shmid = shmget(key, CHUNK_SIZE, 0644 | IPC_CREAT)) == -1) {
        perror("shmget");
        exit(1);
    }
    /* attach to the segment to get a pointer to it: */
    data = shmat(shmid, NULL, 0);
    if (data == (char *)(-1)) {
        perror("shmat");
        exit(1);
    }
    MyData.shmemp[idx] = data;
    idx += 1;
    MyData.n_shmem = idx;
    memset(data, 'a', CHUNK_SIZE);
    // printf("add_shmem: bytes=%luK\n", (idx*CHUNK_SIZE)/1024);
}

int rm_shmem() {
    int shmid;
    int removed = 0;
    for (int idx = 0; idx < CNT; idx++) {
        key_t key = BASE_KEY + idx;
        if ((shmid = shmget(key, CHUNK_SIZE, 0)) == -1)
            continue;
        if (shmctl(shmid, IPC_RMID, NULL) >= 0)
            removed += 1;
    }
    printf("removed %d segments\n", removed);
}


//////////////////////////////// MMAP

int add_mmap() {
    int fd;
    unsigned char *ptr;
    if ((fd = MyData.mm_fd) == 0) {
        fd = open(FILENAME, O_RDWR|O_CREAT, 0666);
        if (fd < 0) {
            perror("open(/tmp/...)");
            exit(1);
        }
        MyData.mm_fd = fd;
        int rv = posix_fallocate(fd, 0, CNT*CHUNK_SIZE);
        if (rv < 0) {
            perror("posix_fallocate()");
            exit(1);
        }
    }
    if ((ptr = MyData.mm_ptr) == NULL) {
        ptr = mmap(NULL, CNT*CHUNK_SIZE, PROT_READ|PROT_WRITE, MAP_SHARED, fd, 0);
        if ((long) ptr == -1) {
            perror("mmap()");
            exit(1);
        }
        MyData.mm_ptr = ptr;
    }
    unsigned char* bottom = &ptr[MyData.n_mmap*CHUNK_SIZE];
    memset(bottom, 'm', CHUNK_SIZE);
    MyData.n_mmap += 1;
    // printf("add_mmap: bytes=%luK\n", MyData.n_mmap*CHUNK_SIZE/1024);
}

int rm_mmap() {
    unlink(FILENAME);
}

//////////////////////////////// MALLOC

int add_malloc() {
    MyData.mallocp[MyData.n_malloc] = malloc(CHUNK_SIZE);
    MyData.n_malloc += 1;
    // printf("add_malloc: bytes=%luK\n", MyData.n_malloc*CHUNK_SIZE/1024);
}

//////////////////////////////// MAIN LOOP STUFF

int loop() {
    void* heap = malloc(CHUNK_SIZE);
    memset(heap, 'h', CHUNK_SIZE);
    char stack[CHUNK_SIZE];
    memset(stack, 'h', CHUNK_SIZE);
    add_shmem();
    add_mmap();
    add_malloc();
    printf("%d: loop=%d, shmem=%luK mmap=%luK stack=%luK heap=%luK\n",
            getpid(),
            LoopCnt+1,
            MyData.n_shmem*CHUNK_SIZE/1024,
            MyData.n_mmap*CHUNK_SIZE/1024,
            MyData.n_mmap*CHUNK_SIZE/1024,
            MyData.n_malloc*CHUNK_SIZE/1024);
    sleep(Args.sleep_sec);
    if (++LoopCnt >= Args.n_loops) {
        exit(0);
    }
    loop(); // NOTE: recurses (to be able to add stack)
}


int main(int argc, char *argv[])
{
    int opt;
    while ((opt = getopt(argc, argv, "qs")) != -1) {
        switch(opt) {
            case 'q': Args.sleep_sec = 1; break;
            case 's': Args.n_loops = 16; break;
            default:
                printf("USE: %s {-qs} # quick,short", argv[0]);
                exit(1);
        }
    }
    memset(&MyData, 0, sizeof(MyData));
    incr_sem();
    loop();

    return 0;
}
