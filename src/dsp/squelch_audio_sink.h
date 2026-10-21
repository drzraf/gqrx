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
 */
#ifndef SQUELCH_AUDIO_SINK_H
#define SQUELCH_AUDIO_SINK_H

#include <gnuradio/sync_block.h>
#include "recording_trigger.h"
#include <chrono>
#include <cstdint>
#include <fstream>
#include <mutex>
#include <string>
#include <vector>

struct squelch_audio_config
{
    std::string directory;
    std::string prefix = "gqrx_rec";
    double sample_rate_hz = 48000.0;
    double center_freq_hz = 0.0;
    float threshold_dbfs = -30.0f;
    double trigger_ms = 10.0;
    double pre_roll_ms = 1000.0;
    double hangover_ms = 1000.0;
    double min_event_ms = 20.0;
    double max_segment_s = 10.0;
    double min_gap_ms = 500.0;
    uint64_t max_total_bytes = 256ULL * 1024ULL * 1024ULL;
};

struct squelch_audio_stats
{
    bool enabled = false;
    bool active = false;
    uint64_t started = 0;
    uint64_t segments = 0;
    uint64_t bytes = 0;
    uint64_t frames = 0;
    uint64_t dropped = 0;
    std::string last_file;
};

class squelch_audio_sink;

#if GNURADIO_VERSION < 0x030900
typedef boost::shared_ptr<squelch_audio_sink> squelch_audio_sink_sptr;
#else
typedef std::shared_ptr<squelch_audio_sink> squelch_audio_sink_sptr;
#endif

squelch_audio_sink_sptr make_squelch_audio_sink();

/*! \brief Fast, bounded stereo audio ring recorder.
 *
 * Taps the demodulated audio (left/right), keeps pre_roll_ms in RAM, triggers
 * on audio amplitude, and writes a centered PCM16 WAV directly. It is the
 * audio counterpart of squelch_iq_sink and has no file-to-file copy step.
 */
class squelch_audio_sink : public gr::sync_block
{
    friend squelch_audio_sink_sptr make_squelch_audio_sink();

protected:
    squelch_audio_sink();

public:
    ~squelch_audio_sink() override;

    int work(int noutput_items,
             gr_vector_const_void_star &input_items,
             gr_vector_void_star &output_items) override;

    void set_config(const squelch_audio_config &config);
    squelch_audio_config get_config() const;
    void set_center_frequency(double freq_hz);
    void set_trigger(const recording_trigger_sptr &trigger);
    void set_enabled(bool enabled);
    bool is_enabled() const;
    void flush();
    squelch_audio_stats get_stats() const;

private:
    enum state_t { STATE_IDLE, STATE_ACTIVE };
    struct frame_t { float left; float right; };

    void recompute_locked();
    void reset_state_locked();
    void push_ring_locked(float left, float right);
    void write_ring_locked();
    void write_block_locked(const float *left, const float *right, int count);
    bool open_segment_locked(float peak_amplitude, bool with_ring);
    void close_segment_locked(int64_t global_frame, bool keep);
    void write_index_locked(bool kept);
    void write_wav_header_locked(uint32_t data_bytes);
    std::string make_filename_locked();

    mutable std::mutex d_mutex;
    recording_trigger_sptr d_trigger;
    uint64_t d_last_trigger = 0;
    squelch_audio_config d_config;
    squelch_audio_stats d_stats;
    state_t d_state = STATE_IDLE;
    bool d_enabled = false;

    float d_threshold_amplitude = 0.0f;
    int64_t d_trigger_frames = 1;
    int64_t d_pre_roll_frames = 0;
    int64_t d_hangover_frames = 0;
    int64_t d_min_event_frames = 0;
    int64_t d_max_segment_frames = 0;
    int64_t d_min_gap_frames = 0;

    std::vector<frame_t> d_ring;
    size_t d_ring_pos = 0;
    size_t d_ring_count = 0;

    int64_t d_frames_processed = 0;
    int64_t d_above_frames = 0;
    int64_t d_last_above_frame = 0;
    int64_t d_gap_until_frame = 0;
    int64_t d_segment_frames = 0;
    float d_segment_peak = 0.0f;
    uint64_t d_segment_seq = 0;

    std::string d_current_path;
    std::string d_segment_start_iso;
    std::ofstream d_stream;
    std::chrono::steady_clock::time_point d_last_flush;
};

#endif /* SQUELCH_AUDIO_SINK_H */
