/**
 * @file shm_ringbuf.h
 * @brief Lock-free SPSC ring buffer over anonymous shared memory (memfd_create + eventfd).
 *
 * Transport design summary
 * ------------------------
 * The producer (gqrx) calls shm_ringbuf_create() which:
 *   1. Creates an anonymous memfd via memfd_create("gqrx_iq_ringbuf", MFD_CLOEXEC).
 *   2. Creates an eventfd(0, EFD_CLOEXEC | EFD_SEMAPHORE | EFD_NONBLOCK) used purely as
 *      a "data available" signal — NOT for data transfer.
 *   3. mmap's the memfd MAP_SHARED and initialises shm_ringbuf_header_t at offset 0.
 *
 * The consumer (e.g. rtl_433) receives the two fds by one of:
 *   A. Inherited fd  — launch rtl_433 as a child process after dup'ing the fds past
 *      O_CLOEXEC (see shm_ringbuf_prepare_child_fds()), then read the fd numbers from
 *      the environment variables GQRX_IQ_MEMFD and GQRX_IQ_EVENTFD.
 *   B. SCM_RIGHTS     — call shm_ringbuf_send_fds() to hand both fds over a short-lived
 *      Unix domain socket at GQRX_IQ_SOCK_DEFAULT (or the path in GQRX_IQ_SOCK).
 *
 * Memory layout (shm_ringbuf_header_t at offset 0, ring data immediately after)
 * ------------------------------------------------------------------------------
 *   offset  0 :  uint32_t magic        = 0x52494E47 ('RING')
 *   offset  4 :  uint32_t version      = 1
 *   offset  8 :  uint64_t head         (producer write index, monotonically increasing)
 *   offset 16 :  uint64_t tail         (consumer read  index, monotonically increasing)
 *   offset 24 :  uint64_t bufsize      (ring data size in bytes, power of 2 recommended)
 *   offset 32 :  uint32_t sample_rate  (Hz)
 *   offset 36 :  uint32_t sample_size  (bytes per IQ pair: 2 = CU8, 4 = CS16)
 *   offset 40 :  [padding to next cache line if any]
 *   offset sizeof(shm_ringbuf_header_t) : ring data bytes
 *
 * IQ sample format: CU8 — interleaved uint8 I then Q, each = value * 127.5 + 127.5,
 *   so full-scale +1.0 → 255, zero → 127-128, full-scale -1.0 → 0.
 *
 * Eventfd notification semantics
 * --------------------------------
 * The producer writes the value 1 (uint64_t) to event_fd after each shm_ringbuf_write()
 * call.  The eventfd is in EFD_SEMAPHORE mode: each read decrements by 1.  The consumer
 * calls poll(event_fd, POLLIN, timeout_ms) then reads 1 token to block until data arrives
 * without busy-spinning.  Overflow of the eventfd counter (UINT64_MAX - 1) is silently
 * ignored (the EAGAIN from write is discarded).
 */

#ifndef SHM_RINGBUF_H
#define SHM_RINGBUF_H

#include <stdint.h>
#include <stddef.h>
#include <stdbool.h>
#include <unistd.h>
#include <sys/stat.h>
#include <sys/mman.h>
#include <fcntl.h>
#include <stdio.h>

#ifdef __cplusplus
extern "C" {
#endif

/* Name passed to memfd_create (visible only in /proc/<pid>/fd/). */
#define MEMFD_RINGBUF_NAME "gqrx_iq_ringbuf"

/*
 * Environment variables used to communicate fd numbers to a child consumer process.
 * The producer sets these before exec'ing the consumer, e.g.:
 *   GQRX_IQ_MEMFD=5 GQRX_IQ_EVENTFD=6 ./rtl_433 ...
 */
#define GQRX_IQ_MEMFD_ENV   "GQRX_IQ_MEMFD"
#define GQRX_IQ_EVENTFD_ENV "GQRX_IQ_EVENTFD"

/*
 * Unix socket path for the SCM_RIGHTS fd handoff to an already-running consumer.
 * Override by setting the GQRX_IQ_SOCK environment variable before starting gqrx.
 * The consumer connects, receives two int fds (memfd, eventfd) via recvmsg SCM_RIGHTS.
 */
#define GQRX_IQ_SOCK_ENV     "GQRX_IQ_SOCK"
#define GQRX_IQ_SOCK_DEFAULT "/tmp/gqrx_iq.sock"

#define SHM_RINGBUF_MAGIC   0x52494E47u  /* 'RING' */
#define SHM_RINGBUF_VERSION 1u

/**
 * Header placed at the beginning of the memfd mapping.
 * All fields use fixed-width types so the binary layout is stable across
 * 32-bit and 64-bit builds and across compiler/platform variations.
 */
typedef struct {
    uint32_t magic;        /**< Must equal SHM_RINGBUF_MAGIC */
    uint32_t version;      /**< Must equal SHM_RINGBUF_VERSION */
    uint64_t head;         /**< Producer write index (bytes, monotonically increasing) */
    uint64_t tail;         /**< Consumer read  index (bytes, monotonically increasing) */
    uint64_t bufsize;      /**< Ring data size in bytes (fixed-width for ABI stability) */
    uint32_t sample_rate;  /**< Sample rate in Hz */
    uint32_t sample_size;  /**< Bytes per IQ pair (2 = CU8, 4 = CS16) */
} shm_ringbuf_header_t;

/** Producer/consumer context.  Zero-initialised before first use. */
typedef struct {
    int      memfd;        /**< Anonymous memfd descriptor (-1 if not open) */
    int      event_fd;     /**< eventfd descriptor for data-available notifications */
    void    *region;       /**< mmap'd region base (NULL if not mapped) */
    size_t   region_size;  /**< Total mmap size including header */
    shm_ringbuf_header_t *hdr;   /**< Pointer into region */
    uint8_t             *buffer; /**< Ring data base (hdr + 1) */
} shm_ringbuf_t;

/**
 * Create and initialise a new memfd ring buffer (producer side).
 * On success ctx->memfd and ctx->event_fd are valid open fds.
 * Both carry O_CLOEXEC; use shm_ringbuf_prepare_child_fds() or
 * shm_ringbuf_send_fds() to hand them to a consumer.
 *
 * @param bufsize      Ring data size in bytes (header is added on top).
 * @param sample_rate  Sample rate in Hz.
 * @param sample_size  Bytes per IQ pair (2 for CU8, 4 for CS16).
 * @param out_ctx      Output context; must point to zero-initialised storage.
 * @return 0 on success, -1 on error (errno set).
 */
int shm_ringbuf_create(size_t bufsize, uint32_t sample_rate, uint32_t sample_size,
                       shm_ringbuf_t *out_ctx);

/**
 * Attach to an existing ring buffer using already-received fds (consumer side).
 * Typically the fds were received via shm_ringbuf_send_fds() or by inheritance.
 *
 * @param memfd     The memfd received from the producer.
 * @param event_fd  The eventfd received from the producer (-1 if not available).
 * @param out_ctx   Output context; must point to zero-initialised storage.
 * @return 0 on success, -1 on error.
 */
int shm_ringbuf_attach(int memfd, int event_fd, shm_ringbuf_t *out_ctx);

/**
 * Unmap and close a ring buffer context.
 * Safe to call on a zero-initialised context (no-op).
 */
int shm_ringbuf_close(shm_ringbuf_t *ctx);

/**
 * Close and zero the context.  With memfd_create there is no filesystem name
 * to unlink; closing the last fd referencing the memfd reclaims the memory.
 * Equivalent to shm_ringbuf_close() but logs a diagnostic message.
 */
void shm_ringbuf_destroy(shm_ringbuf_t *ctx);

/**
 * Duplicate ctx->memfd and ctx->event_fd without O_CLOEXEC so they survive
 * exec() in a child process, and set the environment variables
 * GQRX_IQ_MEMFD and GQRX_IQ_EVENTFD to the new fd numbers.
 * Call this in the parent before fork()+exec().
 *
 * @return 0 on success, -1 on error.
 */
int shm_ringbuf_prepare_child_fds(shm_ringbuf_t *ctx);

/**
 * Hand ctx->memfd and ctx->event_fd to an already-running consumer via a
 * short-lived Unix domain socket using SCM_RIGHTS ancillary data.
 *
 * This function creates a SOCK_STREAM socket at @p path, waits for exactly
 * one incoming connection, sends both fds via recvmsg SCM_RIGHTS, then
 * closes and removes the socket.
 *
 * @param ctx   Initialised producer context.
 * @param path  Unix socket path to listen on, or NULL to use the value of the
 *              GQRX_IQ_SOCK environment variable, or GQRX_IQ_SOCK_DEFAULT.
 * @return 0 on success, -1 on error.
 */
int shm_ringbuf_send_fds(shm_ringbuf_t *ctx, const char *path);

/** Return the number of bytes available to read. */
size_t shm_ringbuf_available(const shm_ringbuf_t *ctx);

/** Return the number of bytes free for writing. */
size_t shm_ringbuf_free(const shm_ringbuf_t *ctx);

/**
 * Write @p len bytes from @p data into the ring buffer.
 * If the buffer does not have @p len bytes free, writes as many as will fit
 * (partial write) and returns the number of bytes actually written.
 * Returns -1 with errno = EAGAIN if the buffer is completely full.
 * After a successful write, signals event_fd once.
 */
ssize_t shm_ringbuf_write(shm_ringbuf_t *ctx, const void *data, size_t len);

/**
 * Read up to @p max_len bytes from the ring buffer into @p out_buf.
 *
 * @param timeout_ms  -1 = block indefinitely; 0 = non-blocking;
 *                    >0 = block for at most this many milliseconds.
 * @return Bytes read (>0), 0 if no data (timeout or non-blocking), -1 on error.
 *
 * When timeout_ms != 0 and the buffer is empty, this function blocks on
 * poll(event_fd) for the requested duration before checking again.
 */
ssize_t shm_ringbuf_read(shm_ringbuf_t *ctx, void *out_buf, size_t max_len,
                         int timeout_ms);

#ifdef __cplusplus
}
#endif

#endif /* SHM_RINGBUF_H */
