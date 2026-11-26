/*
 * Coherence microbenchmarks for studying multicast NoC traffic.
 *
 * Each kernel produces a known sharing pattern so the resulting
 * invalidation fan-out and burst structure can be controlled:
 *
 *   prodcons   one producer rewrites a buffer that all others then read
 *              (every write invalidates P-1 sharers)
 *   readmostly all threads read a shared table; one writer updates an
 *              entry every W reads (sparse wide invalidations)
 *   counter    all threads atomically increment one line (migratory)
 *   lock       test-and-test-and-set spinlock around a short critical
 *              section (spinning readers -> invalidation storms on release)
 *   barrier    centralized sense-reversing barrier with little work in
 *              between (periodic all-to-one / one-to-all bursts)
 *   falseshare threads write different words of the same lines
 *
 * usage: cohmicro <kernel> <threads> <iterations> [param]
 * The parallel region is bracketed by m5_dump_reset_stats() so gem5 stats
 * and the Garnet message trace mark the region of interest.
 */
#define _GNU_SOURCE
#include <pthread.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#ifdef NATIVE_TEST
#define m5_dump_reset_stats(a, b) ((void)0)
#else
#include <gem5/m5ops.h>
#endif

#define LINE 64
#define MAX_THREADS 64
#define BUF_LINES 64
#define TABLE_LINES 256

typedef struct { _Alignas(LINE) atomic_long v; char pad[LINE - sizeof(long)]; } line_t;

static int nthreads;
static long iters;
static long param;

/* centralized sense-reversing barrier */
static line_t bar_count;
static line_t bar_sense;

static void
barrier(int *local_sense)
{
    *local_sense = !*local_sense;
    if (atomic_fetch_add(&bar_count.v, 1) == nthreads - 1) {
        atomic_store(&bar_count.v, 0);
        atomic_store(&bar_sense.v, *local_sense);
    } else {
        while (atomic_load_explicit(&bar_sense.v, memory_order_acquire)
               != *local_sense)
            ;
    }
}

static line_t buf[BUF_LINES];
static line_t table[TABLE_LINES];
static line_t counter;
static line_t lockv;
static line_t shared_data[4];
static _Alignas(LINE) long fs_lines[8][LINE / sizeof(long)];
static volatile long sink[MAX_THREADS * 8];

static void
spin_work(long n)
{
    volatile long x = 0;
    for (long i = 0; i < n; i++)
        x += i;
}

static void
k_prodcons(int id, int *sense)
{
    for (long it = 0; it < iters; it++) {
        if (id == 0)
            for (int l = 0; l < BUF_LINES; l++)
                atomic_store_explicit(&buf[l].v, it * BUF_LINES + l,
                                      memory_order_relaxed);
        barrier(sense);
        long s = 0;
        for (int l = 0; l < BUF_LINES; l++)
            s += atomic_load_explicit(&buf[l].v, memory_order_relaxed);
        sink[id * 8] += s;
        spin_work(param);
        barrier(sense);
    }
}

static void
k_readmostly(int id, int *sense)
{
    long w = param > 0 ? param : 64;
    unsigned seed = id * 2654435761u + 1;
    long s = 0;
    for (long it = 0; it < iters; it++) {
        seed = seed * 1103515245u + 12345u;
        int l = (seed >> 8) % TABLE_LINES;
        if (id == 0 && it % w == 0)
            atomic_fetch_add_explicit(&table[l].v, 1, memory_order_relaxed);
        else
            s += atomic_load_explicit(&table[l].v, memory_order_relaxed);
    }
    sink[id * 8] = s;
    (void)sense;
}

static void
k_counter(int id, int *sense)
{
    for (long it = 0; it < iters; it++) {
        atomic_fetch_add(&counter.v, 1);
        spin_work(param);
    }
    (void)id; (void)sense;
}

static void
k_lock(int id, int *sense)
{
    for (long it = 0; it < iters; it++) {
        for (;;) {
            while (atomic_load_explicit(&lockv.v, memory_order_relaxed))
                ;
            long z = 0;
            if (atomic_compare_exchange_weak(&lockv.v, &z, 1))
                break;
        }
        for (int l = 0; l < 4; l++)
            atomic_store_explicit(&shared_data[l].v,
                atomic_load_explicit(&shared_data[l].v,
                                     memory_order_relaxed) + id,
                memory_order_relaxed);
        atomic_store_explicit(&lockv.v, 0, memory_order_release);
        spin_work(param);
    }
    (void)sense;
}

static void
k_barrier(int id, int *sense)
{
    for (long it = 0; it < iters; it++) {
        spin_work(param);
        barrier(sense);
    }
    (void)id;
}

static void
k_falseshare(int id, int *sense)
{
    long words = LINE / sizeof(long);
    for (long it = 0; it < iters; it++) {
        int l = it % 8;
        fs_lines[l][id % words] += it;
        spin_work(param);
    }
    (void)sense;
}

typedef void (*kernel_fn)(int, int *);
static kernel_fn kernel;
static atomic_int next_id;

static void *
worker(void *arg)
{
    int id = atomic_fetch_add(&next_id, 1);
    int sense = 0;
    (void)arg;
    barrier(&sense);
    if (id == 0)
        m5_dump_reset_stats(0, 0);
    barrier(&sense);
    kernel(id, &sense);
    barrier(&sense);
    if (id == 0)
        m5_dump_reset_stats(0, 0);
    return NULL;
}

int
main(int argc, char **argv)
{
    if (argc < 4) {
        fprintf(stderr, "usage: %s <prodcons|readmostly|counter|lock|"
                "barrier|falseshare> <threads> <iters> [param]\n", argv[0]);
        return 1;
    }
    const char *k = argv[1];
    nthreads = atoi(argv[2]);
    iters = atol(argv[3]);
    param = argc > 4 ? atol(argv[4]) : 0;
    if (nthreads < 1 || nthreads > MAX_THREADS)
        return 1;

    if (!strcmp(k, "prodcons")) kernel = k_prodcons;
    else if (!strcmp(k, "readmostly")) kernel = k_readmostly;
    else if (!strcmp(k, "counter")) kernel = k_counter;
    else if (!strcmp(k, "lock")) kernel = k_lock;
    else if (!strcmp(k, "barrier")) kernel = k_barrier;
    else if (!strcmp(k, "falseshare")) kernel = k_falseshare;
    else { fprintf(stderr, "unknown kernel %s\n", k); return 1; }

    pthread_t tid[MAX_THREADS];
    for (int t = 1; t < nthreads; t++)
        pthread_create(&tid[t], NULL, worker, NULL);
    worker(NULL);
    for (int t = 1; t < nthreads; t++)
        pthread_join(tid[t], NULL);

    printf("%s done: threads=%d iters=%ld param=%ld counter=%ld\n", k,
           nthreads, iters, param, (long)counter.v);
    return 0;
}
