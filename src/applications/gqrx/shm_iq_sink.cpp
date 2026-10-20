#include <gnuradio/io_signature.h>
#include "shm_iq_sink.hpp"
#include "shm_iq_sink_impl.h"

shm_iq_sink::sptr
shm_iq_sink::make(unsigned long sample_rate)
{
#if GNURADIO_VERSION < 0x030900
    return boost::shared_ptr<shm_iq_sink>(new shm_iq_sink_impl(sample_rate));
#else
    return std::shared_ptr<shm_iq_sink>(new shm_iq_sink_impl(sample_rate));
#endif
}
