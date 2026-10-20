#ifndef SHM_RINGBUF_HPP
#define SHM_RINGBUF_HPP

#include <cstddef>
#include <cstdint>
#include <memory>

extern "C" {
#include "shm_ringbuf.h"
}

class ShmRingbuf {
public:
    /**
     * Create a new memfd ring buffer (producer).
     * @param bufsize_bytes  Ring data size in bytes (header added on top).
     * @param sample_rate    Sample rate in Hz.
     * @param sample_size    Bytes per IQ pair (2 = CU8, 4 = CS16).
     * Throws std::runtime_error on failure.
     */
    ShmRingbuf(size_t bufsize_bytes, uint32_t sample_rate, uint32_t sample_size);

    /**
     * Attach to an existing ring buffer by fd (consumer).
     * @param memfd     Anonymous memfd received from the producer.
     * @param event_fd  eventfd received from the producer (-1 if not available).
     * Throws std::runtime_error on failure.
     */
    ShmRingbuf(int memfd, int event_fd);

    ~ShmRingbuf();

    /** Write @p len bytes; returns bytes written or -1 on error (EAGAIN = full). */
    ssize_t write(const void *data, size_t len);

    /** Read up to @p max_len bytes.  @p timeout_ms: -1=block, 0=non-block, >0=timed. */
    ssize_t read(void *out_buf, size_t max_len, int timeout_ms = -1);

    size_t available() const;
    size_t free_space() const;

    uint32_t get_sample_rate() const { return ctx_.hdr ? ctx_.hdr->sample_rate : 0; }
    uint32_t get_sample_size() const { return ctx_.hdr ? ctx_.hdr->sample_size : 0; }

    /** Return the memfd descriptor for fd-passing to a consumer process. */
    int get_memfd()    const { return ctx_.memfd; }
    /** Return the eventfd descriptor for fd-passing to a consumer process. */
    int get_eventfd()  const { return ctx_.event_fd; }

    /**
     * Prepare inherited-fd handoff: dup() both fds past O_CLOEXEC and set
     * GQRX_IQ_MEMFD / GQRX_IQ_EVENTFD environment variables.
     * Call in the parent process before fork()+exec() of the consumer.
     */
    int prepare_child_fds() { return shm_ringbuf_prepare_child_fds(&ctx_); }

    /**
     * Send both fds to an already-running consumer via SCM_RIGHTS on a
     * short-lived Unix domain socket at @p path (NULL = default).
     */
    int send_fds(const char *path = nullptr) {
        return shm_ringbuf_send_fds(&ctx_, path);
    }

private:
    shm_ringbuf_t ctx_;
};

#endif /* SHM_RINGBUF_HPP */
