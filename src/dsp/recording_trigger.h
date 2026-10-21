#ifndef RECORDING_TRIGGER_H
#define RECORDING_TRIGGER_H

#include <atomic>
#include <cstdint>
#include <memory>

/*! Shared, lock-free event marker between the I/Q and audio recorders.
 *
 * The I/Q sink is the timing master. It increments sequence when a segment
 * opens and clears active when the segment closes. The audio sink uses its own
 * pre-roll ring but is gated by the same event, so both recordings start on the
 * same burst and are centered independently.
 */
struct recording_trigger
{
    std::atomic<uint64_t> sequence {0};
    std::atomic<bool> active {false};
};

typedef std::shared_ptr<recording_trigger> recording_trigger_sptr;

inline recording_trigger_sptr make_recording_trigger()
{
    return std::make_shared<recording_trigger>();
}

#endif /* RECORDING_TRIGGER_H */
