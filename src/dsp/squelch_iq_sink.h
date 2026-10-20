/* -*- c++ -*- */
/*
 * Gqrx SDR: Software defined radio receiver powered by GNU Radio and Qt
 *           https://gqrx.dk/
 *
 * Copyright 2026 Gqrx contributors
 *
 * Gqrx is free software; you can redistribute it and/or modify
 * it under the terms of the GNU General Public License as published by
 * the Free Software Foundation; either version 3, or (at your option)
 * any later version.
 *
 * Gqrx is distributed in the hope that it will be useful,
 * but WITHOUT ANY WARRANTY; without even the implied warranty of
 * MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
 * GNU General Public License for more details.
 *
 * You should have received a copy of the GNU General Public License
 * along with Gqrx; see the file COPYING.  If not, write to
 * the Free Software Foundation, Inc., 51 Franklin Street,
 * Boston, MA 02110-1301, USA.
 */
#ifndef SQUELCH_IQ_SINK_H
#define SQUELCH_IQ_SINK_H

#include <gnuradio/sync_block.h>
#include <chrono>
#include <cstdint>
#include <fstream>
#include <mutex>
#include <string>
#include <vector>

/*! Configuration for the squelch-gated I/Q evidence recorder. */
struct squelch_iq_config
{
    std::string directory;          /*!< Output directory (created if needed). */
    std::string prefix = "gqrx_evt";
    double sample_rate_hz = 0.0;    /*!< Input rate at the sink (after decimation). */
    double center_freq_hz = 0.0;    /*!< Hardware/FFT center frequency. */
    float threshold_db = -60.0f;    /*!< Trigger threshold in dBFS. */
    double trigger_ms = 0.2;        /*!< Consecutive above-threshold time to trigger. */
    double pre_roll_ms = 250.0;     /*!< Samples kept before the trigger. */
    double hangover_ms = 500.0;     /*!< Keep recording this long after the signal drops. */
    double min_event_ms = 50.0;     /*!< Discard shorter segments. */
    double max_segment_s = 30.0;    /*!< Split long segments at this duration. */
    double min_gap_ms = 1000.0;     /*!< Suppress re-triggering after a close. */
    bool sigmf = false;             /*!< Write SigMF data + metadata instead of raw. */
};

struct squelch_iq_stats
{
    bool enabled = false;
    bool active = false;
    uint64_t started = 0;           /*!< Segments opened (including later dropped). */
    uint64_t segments = 0;          /*!< Completed, kept segments. */
    uint64_t bytes = 0;             /*!< Payload bytes written. */
    uint64_t samples = 0;           /*!< Payload samples written. */
    uint64_t dropped = 0;           /*!< Segments discarded by min_event. */
    std::string last_file;
};

class squelch_iq_sink;

#if GNURADIO_VERSION < 0x030900
typedef boost::shared_ptr<squelch_iq_sink> squelch_iq_sink_sptr;
#else
typedef std::shared_ptr<squelch_iq_sink> squelch_iq_sink_sptr;
#endif

squelch_iq_sink_sptr make_squelch_iq_sink();

/*! \brief Fast, bounded, squelch-gated I/Q evidence sink.
 *
 * The sink watches the instantaneous power of the complex input. When the
 * signal stays above the threshold for trigger_ms it opens a segment and
 * writes the pre-roll ring, then the live samples. A hangover keeps the
 * segment open through gaps, min_event rejects spikes, max_segment_s splits
 * long transmissions, and min_gap_ms prevents file floods.
 *
 * The sink deliberately writes on the GNU Radio thread and has no queue. At
 * 250-500 kSps the write cost is small; the downstream recorder can choose a
 * decimated rate. A future version can add a writer thread without changing
 * the trigger logic.
 */
class squelch_iq_sink : public gr::sync_block
{
    friend squelch_iq_sink_sptr make_squelch_iq_sink();

protected:
    squelch_iq_sink();

public:
    ~squelch_iq_sink() override;

    int work(int noutput_items,
             gr_vector_const_void_star &input_items,
             gr_vector_void_star &output_items) override;

    void set_config(const squelch_iq_config &config);
    squelch_iq_config get_config() const;
    void set_center_frequency(double freq_hz);
    void set_sample_rate(double rate_hz);

    void set_enabled(bool enabled);
    bool is_enabled() const;
    void flush();
    squelch_iq_stats get_stats() const;

private:
    enum state_t { STATE_IDLE, STATE_ACTIVE };

    void recompute_locked();
    void reset_state_locked();
    void push_ring_locked(const gr_complex &sample);
    void write_ring_locked();
    void write_block_locked(const gr_complex *samples, int count);
    bool open_segment_locked(int64_t global_sample, float peak_power, bool with_ring);
    void close_segment_locked(int64_t global_sample, bool keep);
    void write_index_locked(bool kept);
    void write_sigmf_meta_locked(bool kept);
    std::string make_filename_locked(const std::string &suffix);

    mutable std::mutex d_mutex;
    squelch_iq_config d_config;
    squelch_iq_stats d_stats;
    state_t d_state = STATE_IDLE;
    bool d_enabled = false;

    float d_threshold_power = 0.0f;
    int64_t d_trigger_samples = 1;
    int64_t d_pre_roll_samples = 0;
    int64_t d_hangover_samples = 0;
    int64_t d_min_event_samples = 0;
    int64_t d_max_segment_samples = 0;
    int64_t d_min_gap_samples = 0;

    std::vector<gr_complex> d_ring;
    size_t d_ring_pos = 0;
    size_t d_ring_count = 0;

    int64_t d_samples_processed = 0;
    int64_t d_above_samples = 0;
    int64_t d_last_above_sample = 0;
    int64_t d_gap_until_sample = 0;
    int64_t d_segment_samples = 0;
    float d_segment_peak = 0.0f;
    uint64_t d_segment_seq = 0;

    std::string d_current_path;
    std::string d_current_meta;
    std::string d_segment_start_iso;
    std::ofstream d_stream;
    std::chrono::steady_clock::time_point d_last_flush;
};

#endif /* SQUELCH_IQ_SINK_H */
