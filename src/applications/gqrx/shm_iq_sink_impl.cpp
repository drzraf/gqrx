#include "shm_iq_sink_impl.h"
#include "shm_ringbuf.hpp"
#include <iostream>
#include <cstring>
#include <algorithm>
#include <ctime>

shm_iq_sink_impl::shm_iq_sink_impl(unsigned long sample_rate)
    : gr::sync_block("shm_iq_sink",
                     gr::io_signature::make(1, 2, sizeof(gr_complex)),
                     gr::io_signature::make(0, 0, 0)),
      last_overflow_warn_(0)
{
    try {
        /* 32 MB ring buffer; 2 bytes per CU8 IQ pair */
        ringbuf_ = std::make_unique<ShmRingbuf>(32 * 1024 * 1024, sample_rate, 2);

        std::cout << "\n================================================\n"
                  << "GNU Radio Shared Memory IQ Sink initialized\n"
                  << "  Sample rate : " << sample_rate << " Hz\n"
                  << "  Transport   : memfd_create(\"" MEMFD_RINGBUF_NAME "\")"
                     " + eventfd\n"
                  << "  Sample fmt  : CU8 (interleaved uint8 I/Q, "
                     "value = sample * 127.5 + 127.5)\n"
                  << "\nConsumer fd discovery:\n"
                  << "  Child process  : set " GQRX_IQ_MEMFD_ENV "=<fd> "
                     GQRX_IQ_EVENTFD_ENV "=<fd> before exec\n"
                  << "  Unrelated proc : connect to Unix socket "
                     GQRX_IQ_SOCK_DEFAULT " (or $" GQRX_IQ_SOCK_ENV ")\n"
                  << "================================================\n"
                  << std::endl;

        /* Hand the memfd/eventfd pair to an external, already-running
         * consumer (e.g. rtl_433 started with `-J sock:<path>`) via
         * SCM_RIGHTS on a short-lived Unix domain socket.
         *
         * shm_ringbuf_send_fds() blocks on accept() until a consumer
         * connects (or indefinitely if none ever does), so it must run on
         * a background thread rather than the constructor's calling
         * thread -- otherwise starting IQ recording would hang gqrx until
         * a consumer attached.
         *
         * This thread is intentionally detached: if the sink is destroyed
         * before a consumer connects, the accept() call simply never
         * returns and the thread is reaped at process exit. A future
         * improvement would be to make the accept() interruptible (e.g.
         * via a self-pipe/eventfd added to a poll() loop) so it can be
         * cancelled cleanly on stop_iq_recording(). */
        fd_handoff_thread_ = std::thread([this]() {
            std::cerr << "[SHM] fd_handoff_thread_ starting (30 s consumer timeout)..." << std::endl;
            if (ringbuf_->send_fds() != 0) {
                std::cerr << "[SHM] Warning: fd handoff via "
                             GQRX_IQ_SOCK_DEFAULT " failed or no consumer "
                             "connected" << std::endl;
            }
        });
    } catch (const std::exception &e) {
        std::cerr << "Failed to create memfd ring buffer: " << e.what() << std::endl;
        throw;
    }
}

shm_iq_sink_impl::~shm_iq_sink_impl()
{
    if (fd_handoff_thread_.joinable())
        fd_handoff_thread_.detach();
    ringbuf_.reset();
}

int shm_iq_sink_impl::work(int noutput_items,
                            gr_vector_const_void_star& input_items,
                            gr_vector_void_star& output_items)
{
    const gr_complex *in = (const gr_complex *)input_items[0];

    /* Ensure batch buffer is large enough (2 bytes per IQ pair) */
    const size_t needed = (size_t)noutput_items * 2;
    if (batch_buf_.size() < needed)
        batch_buf_.resize(needed);

    /* Keep the CU8 conversion linear. The producer taps raw gqrx samples;
     * applying a large fixed gain here would clip strong signals and make
     * the stream unusable for decoder calibration. */
    constexpr float SHM_CU8_GAIN = 1.0f;

    /* Batch-convert entire block to CU8.
     * Standard symmetric centering: value * 127.5 + 127.5
     *   full-scale +1.0 → 255, zero carrier → 127.5 (rounds to 127 or 128),
     *   full-scale -1.0 → 0.
     * This matches RTL-SDR hardware output and rtl_433 CU8 expectations. */
    for (int i = 0; i < noutput_items; i++) {
        float iv = std::max(-1.0f, std::min(1.0f, in[i].real() * SHM_CU8_GAIN));
        float qv = std::max(-1.0f, std::min(1.0f, in[i].imag() * SHM_CU8_GAIN));
        batch_buf_[2 * i]     = (uint8_t)(iv * 127.5f + 127.5f);
        batch_buf_[2 * i + 1] = (uint8_t)(qv * 127.5f + 127.5f);
    }

    /* Write the full converted block to the ring buffer, retrying on partial
     * writes so we never leave a torn I/Q pair in the stream.
     *
     * shm_ringbuf_write() may return a *partial* byte count (less than
     * requested) when the ring buffer doesn't currently have enough free
     * space for the whole block -- it is not an error, just "wrote what fit".
     * The previous implementation treated any non-negative return as
     * success and silently discarded the unwritten remainder. Because CU8
     * samples are 2 bytes/pair, a partial write that stops on an odd byte
     * boundary splits an I/Q pair across the gap; every subsequent sample
     * in the ring buffer is then misaligned by one byte until the consumer
     * (rtl_433) somehow resynchronizes -- which it cannot do, since there
     * is no framing/sync marker in the raw CU8 stream. This is consistent
     * with a signal that "briefly works, then degrades to noise": the
     * first partial write silently corrupts everything downstream of it.
     *
     * Fix: loop until either (a) the entire block has been written, or
     * (b) the buffer is completely full (EAGAIN) and we deliberately drop
     * the *whole remaining* block instead of a byte-unaligned fragment of
     * it. Dropping whole blocks preserves I/Q pair alignment for all
     * future writes; dropping partial bytes does not. */
    size_t total_written = 0;
    while (total_written < needed) {
        ssize_t written = ringbuf_->write(batch_buf_.data() + total_written,
                                          needed - total_written);
        if (written < 0) {
            /* Buffer full (EAGAIN) or real error: stop here. Whatever
             * portion of this block was already written is guaranteed to
             * be a whole number of I/Q pairs, since we only ever advance
             * total_written by amounts that were themselves confirmed
             * written below -- so we never re-enter the loop mid-pair. */
            break;
        }
        total_written += (size_t)written;
    }

    if (total_written < needed) {
        /* Ring buffer full — non-fatal overflow: drop the unwritten
         * remainder of this block and emit a rate-limited warning (at
         * most once per second). Note total_written may be > 0 here; if
         * it is not itself a multiple of 2 (one CU8 IQ pair), pad/discard
         * down to the last whole pair boundary before giving up, so we
         * never leave a torn sample at the tail of the ring buffer either. */
        size_t aligned_written = total_written - (total_written % 2);
        if (aligned_written != total_written) {
            /* This should not normally happen since 'needed' and each
             * successful partial write from shm_ringbuf_write() operate on
             * a byte granularity that could in principle split a pair --
             * guard against it explicitly rather than assume. */
            total_written = aligned_written;
        }

        time_t now = time(nullptr);
        if (now != last_overflow_warn_) {
            std::cerr << "[SHM] Ring buffer full, dropping "
                      << (needed - total_written) << " of " << needed
                      << " bytes (" << noutput_items << "-sample block)"
                      << std::endl;
            last_overflow_warn_ = now;
        }
    }

    /* Always acknowledge consumption to keep GNU Radio's scheduler happy */
    return noutput_items;
}
