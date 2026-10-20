#ifndef INCLUDED_SHM_IQ_SINK_IMPL_H
#define INCLUDED_SHM_IQ_SINK_IMPL_H

#include "shm_iq_sink.hpp"
#include <memory>
#include <vector>
#include <ctime>
#include <thread>

class ShmRingbuf;

class shm_iq_sink_impl : public shm_iq_sink {
public:
    explicit shm_iq_sink_impl(unsigned long sample_rate);
    ~shm_iq_sink_impl();

    int work(int noutput_items,
             gr_vector_const_void_star& input_items,
             gr_vector_void_star& output_items);

private:
    std::unique_ptr<ShmRingbuf> ringbuf_;
    std::vector<uint8_t>        batch_buf_;     /**< Reusable CU8 conversion buffer */
    time_t                      last_overflow_warn_; /**< For rate-limiting overflow logs */
    std::thread                 fd_handoff_thread_;  /**< Background SCM_RIGHTS fd sender */
};

#endif /* INCLUDED_SHM_IQ_SINK_IMPL_H */
