/*
 * shm_ringbuf.cpp — lock-free SPSC ring buffer over memfd_create + eventfd.
 *
 * See include/shm_ringbuf.h for the full design/protocol description.
 */

#include "shm_ringbuf.h"

#include <string.h>
#include <errno.h>
#include <stdlib.h>
#include <inttypes.h>

#include <sys/eventfd.h>
#include <sys/socket.h>
#include <sys/syscall.h>
#include <sys/un.h>
#include <sys/stat.h>
#include <poll.h>

/* ---------------------------------------------------------------------------
 * memfd_create compat — use glibc's declaration when available (glibc >= 2.27,
 * where it appears via <bits/mman-shared.h> pulled in by <sys/mman.h>).
 * On older toolchains fall back to a direct syscall.
 * MFD_CLOEXEC may not be defined on older kernel headers; define it if absent.
 * -------------------------------------------------------------------------*/
#ifndef MFD_CLOEXEC
# define MFD_CLOEXEC 1u
#endif
/* If glibc >= 2.27 memfd_create is already declared via <sys/mman.h> → nothing
 * to do.  Otherwise provide a thin syscall wrapper under a private name to
 * avoid clashing with a later extern declaration. */
#if !defined(__GLIBC__) || (__GLIBC__ == 2 && __GLIBC_MINOR__ < 27)
static inline int memfd_create(const char *name, unsigned int flags)
{
    return (int)syscall(SYS_memfd_create, name, flags);
}
#endif

/* ---------------------------------------------------------------------------
 * shm_ringbuf_create
 * -------------------------------------------------------------------------*/
int shm_ringbuf_create(size_t bufsize, uint32_t sample_rate, uint32_t sample_size,
                       shm_ringbuf_t *out_ctx)
{
    if (!out_ctx || bufsize == 0) {
        errno = EINVAL;
        return -1;
    }

    memset(out_ctx, 0, sizeof(*out_ctx));
    out_ctx->memfd    = -1;
    out_ctx->event_fd = -1;

    /* Create anonymous shared memory fd */
    int mfd = memfd_create(MEMFD_RINGBUF_NAME, MFD_CLOEXEC);
    if (mfd < 0) {
        perror("shm_ringbuf: memfd_create");
        return -1;
    }

    size_t total_size = sizeof(shm_ringbuf_header_t) + bufsize;
    if (ftruncate(mfd, (off_t)total_size) < 0) {
        perror("shm_ringbuf: ftruncate");
        close(mfd);
        return -1;
    }

    void *region = mmap(NULL, total_size, PROT_READ | PROT_WRITE, MAP_SHARED, mfd, 0);
    if (region == MAP_FAILED) {
        perror("shm_ringbuf: mmap");
        close(mfd);
        return -1;
    }

    /* Producer→consumer "data available" notification fd.
     * EFD_SEMAPHORE: each read drains exactly 1 token.
     * EFD_NONBLOCK: writes in shm_ringbuf_write() never block. */
    int efd = eventfd(0, EFD_CLOEXEC | EFD_SEMAPHORE | EFD_NONBLOCK);
    if (efd < 0) {
        perror("shm_ringbuf: eventfd");
        munmap(region, total_size);
        close(mfd);
        return -1;
    }

    out_ctx->memfd       = mfd;
    out_ctx->event_fd    = efd;
    out_ctx->region      = region;
    out_ctx->region_size = total_size;
    out_ctx->hdr         = (shm_ringbuf_header_t *)region;
    out_ctx->buffer      = (uint8_t *)region + sizeof(shm_ringbuf_header_t);

    out_ctx->hdr->magic       = SHM_RINGBUF_MAGIC;
    out_ctx->hdr->version     = SHM_RINGBUF_VERSION;
    out_ctx->hdr->head        = 0;
    out_ctx->hdr->tail        = 0;
    out_ctx->hdr->bufsize     = (uint64_t)bufsize;
    out_ctx->hdr->sample_rate = sample_rate;
    out_ctx->hdr->sample_size = sample_size;

    fprintf(stderr,
            "[SHM] Created ring buffer: memfd=%d eventfd=%d "
            "(bufsize=%zu sample_rate=%u sample_size=%u)\n",
            mfd, efd, bufsize, sample_rate, sample_size);
    fprintf(stderr,
            "[SHM] Consumer fd discovery:\n"
            "[SHM]   Child  process: %s=%d %s=%d (set before exec)\n"
            "[SHM]   Unrelated proc: connect to Unix socket %s (or $%s) "
            "after calling shm_ringbuf_send_fds()\n",
            GQRX_IQ_MEMFD_ENV,   mfd,
            GQRX_IQ_EVENTFD_ENV, efd,
            GQRX_IQ_SOCK_DEFAULT, GQRX_IQ_SOCK_ENV);

    return 0;
}

/* ---------------------------------------------------------------------------
 * shm_ringbuf_attach  (consumer side)
 * -------------------------------------------------------------------------*/
int shm_ringbuf_attach(int memfd, int event_fd, shm_ringbuf_t *out_ctx)
{
    if (!out_ctx || memfd < 0) {
        errno = EINVAL;
        return -1;
    }

    memset(out_ctx, 0, sizeof(*out_ctx));
    out_ctx->memfd    = memfd;
    out_ctx->event_fd = event_fd;

    struct stat sb;
    if (fstat(memfd, &sb) < 0) {
        perror("shm_ringbuf: fstat");
        return -1;
    }
    size_t total_size = (size_t)sb.st_size;

    void *region = mmap(NULL, total_size, PROT_READ | PROT_WRITE, MAP_SHARED, memfd, 0);
    if (region == MAP_FAILED) {
        perror("shm_ringbuf: mmap");
        return -1;
    }

    out_ctx->region      = region;
    out_ctx->region_size = total_size;
    out_ctx->hdr         = (shm_ringbuf_header_t *)region;
    out_ctx->buffer      = (uint8_t *)region + sizeof(shm_ringbuf_header_t);

    if (out_ctx->hdr->magic != SHM_RINGBUF_MAGIC) {
        fprintf(stderr, "[SHM] Bad magic 0x%x (expected 0x%x)\n",
                out_ctx->hdr->magic, SHM_RINGBUF_MAGIC);
        munmap(region, total_size);
        return -1;
    }

    fprintf(stderr,
            "[SHM] Attached: memfd=%d eventfd=%d "
            "(bufsize=%" PRIu64 " sample_rate=%u sample_size=%u)\n",
            memfd, event_fd,
            out_ctx->hdr->bufsize,
            out_ctx->hdr->sample_rate,
            out_ctx->hdr->sample_size);
    return 0;
}

/* ---------------------------------------------------------------------------
 * shm_ringbuf_close
 * -------------------------------------------------------------------------*/
int shm_ringbuf_close(shm_ringbuf_t *ctx)
{
    if (!ctx || ctx->region == NULL)
        return 0;

    if (munmap(ctx->region, ctx->region_size) < 0)
        perror("shm_ringbuf: munmap");

    if (ctx->memfd >= 0)
        close(ctx->memfd);
    if (ctx->event_fd >= 0)
        close(ctx->event_fd);

    memset(ctx, 0, sizeof(*ctx));
    ctx->memfd    = -1;
    ctx->event_fd = -1;
    return 0;
}

/* ---------------------------------------------------------------------------
 * shm_ringbuf_destroy
 * -------------------------------------------------------------------------*/
void shm_ringbuf_destroy(shm_ringbuf_t *ctx)
{
    /* With memfd_create there is no filesystem name to unlink.
     * Closing the last fd referencing the memfd reclaims the memory. */
    shm_ringbuf_close(ctx);
    fprintf(stderr, "[SHM] Destroyed memfd ring buffer\n");
}

/* ---------------------------------------------------------------------------
 * shm_ringbuf_prepare_child_fds
 * -------------------------------------------------------------------------*/
int shm_ringbuf_prepare_child_fds(shm_ringbuf_t *ctx)
{
    if (!ctx || ctx->memfd < 0 || ctx->event_fd < 0) {
        errno = EINVAL;
        return -1;
    }

    /* dup() the fds without FD_CLOEXEC so they survive exec() */
    int m2 = dup(ctx->memfd);
    if (m2 < 0) {
        perror("shm_ringbuf: dup memfd");
        /* Clear any stale env vars from a previous (failed) call */
        unsetenv(GQRX_IQ_MEMFD_ENV);
        unsetenv(GQRX_IQ_EVENTFD_ENV);
        return -1;
    }
    int e2 = dup(ctx->event_fd);
    if (e2 < 0) {
        perror("shm_ringbuf: dup eventfd");
        close(m2);
        unsetenv(GQRX_IQ_MEMFD_ENV);
        unsetenv(GQRX_IQ_EVENTFD_ENV);
        return -1;
    }

    char buf[32];
    snprintf(buf, sizeof(buf), "%d", m2);
    setenv(GQRX_IQ_MEMFD_ENV, buf, 1);
    snprintf(buf, sizeof(buf), "%d", e2);
    setenv(GQRX_IQ_EVENTFD_ENV, buf, 1);

    fprintf(stderr, "[SHM] Child fds prepared: %s=%d %s=%d\n",
            GQRX_IQ_MEMFD_ENV, m2, GQRX_IQ_EVENTFD_ENV, e2);
    return 0;
}

/* ---------------------------------------------------------------------------
 * shm_ringbuf_send_fds  (SCM_RIGHTS handoff to an unrelated process)
 * -------------------------------------------------------------------------*/
int shm_ringbuf_send_fds(shm_ringbuf_t *ctx, const char *path)
{
    if (!ctx || ctx->memfd < 0 || ctx->event_fd < 0) {
        errno = EINVAL;
        return -1;
    }

    if (!path) {
        path = getenv(GQRX_IQ_SOCK_ENV);
        if (!path)
            path = GQRX_IQ_SOCK_DEFAULT;
    }

    int srv = socket(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0);
    if (srv < 0) { perror("shm_ringbuf: socket"); return -1; }

    struct sockaddr_un addr;
    memset(&addr, 0, sizeof(addr));
    addr.sun_family = AF_UNIX;
    strncpy(addr.sun_path, path, sizeof(addr.sun_path) - 1);

    /* Remove any stale socket file */
    unlink(path);

    if (bind(srv, (struct sockaddr *)&addr, sizeof(addr)) < 0) {
        perror("shm_ringbuf: bind");
        close(srv);
        return -1;
    }
    if (listen(srv, 1) < 0) {
        perror("shm_ringbuf: listen");
        close(srv);
        unlink(path);
        return -1;
    }
    /* The socket only hands over the IQ fds and lives for one short-lived
     * handoff. Allow a local consumer running as a different uid (common in
     * containers/sandboxes) to connect. */
    chmod(path, 0666);

    fprintf(stderr, "[SHM] Waiting for consumer on %s ...\n", path);
    struct pollfd pfd;
    pfd.fd = srv;
    pfd.events = POLLIN;
    pfd.revents = 0;
    int pr = poll(&pfd, 1, 30000);
    if (pr <= 0) {
        fprintf(stderr, "[SHM] No consumer connected within 30 s\n");
        close(srv);
        unlink(path);
        errno = ETIMEDOUT;
        return -1;
    }
    int cli = accept(srv, NULL, NULL);
    close(srv);
    unlink(path);

    if (cli < 0) { perror("shm_ringbuf: accept"); return -1; }

    /* Build SCM_RIGHTS ancillary message carrying both fds */
    int fds[2] = { ctx->memfd, ctx->event_fd };
    char dummy[1] = { 0 };
    struct iovec iov = { .iov_base = dummy, .iov_len = 1 };

    union {
        struct cmsghdr cm;
        char ctrl[CMSG_SPACE(2 * sizeof(int))];
    } ctrl_un;
    memset(&ctrl_un, 0, sizeof(ctrl_un));

    struct msghdr msg;
    memset(&msg, 0, sizeof(msg));
    msg.msg_iov        = &iov;
    msg.msg_iovlen     = 1;
    msg.msg_control    = ctrl_un.ctrl;
    msg.msg_controllen = sizeof(ctrl_un.ctrl);

    struct cmsghdr *cmsg = CMSG_FIRSTHDR(&msg);
    cmsg->cmsg_len   = CMSG_LEN(2 * sizeof(int));
    cmsg->cmsg_level = SOL_SOCKET;
    cmsg->cmsg_type  = SCM_RIGHTS;
    memcpy(CMSG_DATA(cmsg), fds, 2 * sizeof(int));

    int ret = 0;
    if (sendmsg(cli, &msg, 0) < 0) {
        perror("shm_ringbuf: sendmsg");
        ret = -1;
    } else {
        fprintf(stderr, "[SHM] Sent memfd=%d eventfd=%d to consumer via %s\n",
                ctx->memfd, ctx->event_fd, path);
    }

    close(cli);
    return ret;
}

/* ---------------------------------------------------------------------------
 * shm_ringbuf_available / shm_ringbuf_free
 * -------------------------------------------------------------------------*/
size_t shm_ringbuf_available(const shm_ringbuf_t *ctx)
{
    if (!ctx || !ctx->hdr) return 0;
    uint64_t head = __atomic_load_n(&ctx->hdr->head, __ATOMIC_ACQUIRE);
    uint64_t tail = __atomic_load_n(&ctx->hdr->tail, __ATOMIC_ACQUIRE);
    return (size_t)(head - tail);
}

size_t shm_ringbuf_free(const shm_ringbuf_t *ctx)
{
    if (!ctx || !ctx->hdr) return 0;
    return (size_t)(ctx->hdr->bufsize - (uint64_t)shm_ringbuf_available(ctx));
}

/* ---------------------------------------------------------------------------
 * shm_ringbuf_write
 * -------------------------------------------------------------------------*/
ssize_t shm_ringbuf_write(shm_ringbuf_t *ctx, const void *data, size_t len)
{
    if (!ctx || !ctx->hdr || !data || len == 0) {
        errno = EINVAL;
        return -1;
    }

    uint64_t bufsize  = ctx->hdr->bufsize;
    size_t   free_sp  = shm_ringbuf_free(ctx);

    if (free_sp == 0) {
        errno = EAGAIN;
        return -1;  /* buffer full */
    }

    size_t to_write = (len <= free_sp) ? len : free_sp;

    uint64_t head      = __atomic_load_n(&ctx->hdr->head, __ATOMIC_ACQUIRE);
    size_t   write_pos = (size_t)(head % bufsize);
    size_t   to_end    = (size_t)bufsize - write_pos;

    if (to_write <= to_end) {
        memcpy(&ctx->buffer[write_pos], data, to_write);
    } else {
        memcpy(&ctx->buffer[write_pos], data, to_end);
        memcpy(&ctx->buffer[0], (const uint8_t *)data + to_end, to_write - to_end);
    }

    __atomic_add_fetch(&ctx->hdr->head, to_write, __ATOMIC_RELEASE);

    /* Signal consumer: one token per write call.  Silently ignore EAGAIN if
     * the eventfd counter has saturated at UINT64_MAX - 1. */
    if (ctx->event_fd >= 0) {
        uint64_t one = 1;
        ssize_t n = write(ctx->event_fd, &one, sizeof(one));
        (void)n;
    }

    return (ssize_t)to_write;
}

/* ---------------------------------------------------------------------------
 * shm_ringbuf_read
 * -------------------------------------------------------------------------*/
ssize_t shm_ringbuf_read(shm_ringbuf_t *ctx, void *out_buf, size_t max_len,
                         int timeout_ms)
{
    if (!ctx || !ctx->hdr || !out_buf || max_len == 0) {
        errno = EINVAL;
        return -1;
    }

    /* Block until data is available (or timeout) using poll() on eventfd */
    if (ctx->event_fd >= 0 && shm_ringbuf_available(ctx) == 0 && timeout_ms != 0) {
        struct pollfd pfd;
        pfd.fd      = ctx->event_fd;
        pfd.events  = POLLIN;
        pfd.revents = 0;

        int r = poll(&pfd, 1, timeout_ms);   /* timeout_ms == -1 → infinite */
        if (r <= 0)
            return 0;   /* timeout or signal */

        /* Drain one semaphore token */
        uint64_t val;
        ssize_t n = read(ctx->event_fd, &val, sizeof(val));
        (void)n;
    }

    uint64_t bufsize  = ctx->hdr->bufsize;
    size_t   avail    = shm_ringbuf_available(ctx);

    if (avail == 0)
        return 0;

    size_t   to_read  = (max_len <= avail) ? max_len : avail;
    uint64_t tail     = __atomic_load_n(&ctx->hdr->tail, __ATOMIC_ACQUIRE);
    size_t   read_pos = (size_t)(tail % bufsize);
    size_t   to_end   = (size_t)bufsize - read_pos;

    if (to_read <= to_end) {
        memcpy(out_buf, &ctx->buffer[read_pos], to_read);
    } else {
        memcpy(out_buf, &ctx->buffer[read_pos], to_end);
        memcpy((uint8_t *)out_buf + to_end, &ctx->buffer[0], to_read - to_end);
    }

    __atomic_add_fetch(&ctx->hdr->tail, to_read, __ATOMIC_RELEASE);

    return (ssize_t)to_read;
}
