#ifndef INCLUDED_SHM_IQ_SINK_HPP
#define INCLUDED_SHM_IQ_SINK_HPP

#include <gnuradio/sync_block.h>

class shm_iq_sink : virtual public gr::sync_block {
public:
#if GNURADIO_VERSION < 0x030900
    typedef boost::shared_ptr<shm_iq_sink> sptr;
#else
    typedef std::shared_ptr<shm_iq_sink> sptr;
#endif

    /**
     * \brief Return a shared_ptr to a new instance of shm_iq_sink.
     */
    static sptr make(unsigned long sample_rate);
};

#endif /* INCLUDED_SHM_IQ_SINK_HPP */
