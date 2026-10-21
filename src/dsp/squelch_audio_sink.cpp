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
#include "squelch_audio_sink.h"

#include <gnuradio/io_signature.h>

#include <QDateTime>
#include <QDir>
#include <QFile>
#include <QFileInfo>
#include <QJsonDocument>
#include <QJsonObject>

#include <algorithm>
#include <cmath>
#include <cstring>
#include <iostream>

static void put_u16(char *p, uint16_t value)
{
    p[0] = static_cast<char>(value & 0xff);
    p[1] = static_cast<char>((value >> 8) & 0xff);
}

static void put_u32(char *p, uint32_t value)
{
    p[0] = static_cast<char>(value & 0xff);
    p[1] = static_cast<char>((value >> 8) & 0xff);
    p[2] = static_cast<char>((value >> 16) & 0xff);
    p[3] = static_cast<char>((value >> 24) & 0xff);
}

squelch_audio_sink_sptr make_squelch_audio_sink()
{
    return gnuradio::get_initial_sptr(new squelch_audio_sink());
}

squelch_audio_sink::squelch_audio_sink()
    : gr::sync_block("squelch_audio_sink",
                     gr::io_signature::make(2, 2, sizeof(float)),
                     gr::io_signature::make(0, 0, 0)),
      d_last_flush(std::chrono::steady_clock::now())
{
}

squelch_audio_sink::~squelch_audio_sink()
{
    std::lock_guard<std::mutex> lock(d_mutex);
    if (d_state == STATE_ACTIVE)
        close_segment_locked(d_frames_processed, true);
}

void squelch_audio_sink::recompute_locked()
{
    const double rate = d_config.sample_rate_hz;
    auto ms_to_frames = [rate](double ms) -> int64_t {
        if (rate <= 0.0 || ms <= 0.0)
            return 0;
        return static_cast<int64_t>(std::llround(rate * ms / 1000.0));
    };

    d_threshold_amplitude = static_cast<float>(
        std::pow(10.0, static_cast<double>(d_config.threshold_dbfs) / 20.0));
    d_trigger_frames = std::max<int64_t>(1, ms_to_frames(d_config.trigger_ms));
    d_pre_roll_frames = ms_to_frames(d_config.pre_roll_ms);
    d_hangover_frames = ms_to_frames(d_config.hangover_ms);
    d_min_event_frames = ms_to_frames(d_config.min_event_ms);
    d_max_segment_frames = ms_to_frames(d_config.max_segment_s * 1000.0);
    d_min_gap_frames = ms_to_frames(d_config.min_gap_ms);

    d_ring.assign(static_cast<size_t>(std::max<int64_t>(1, d_pre_roll_frames)),
                  frame_t {0.0f, 0.0f});
}

void squelch_audio_sink::reset_state_locked()
{
    d_state = STATE_IDLE;
    d_ring_pos = 0;
    d_ring_count = 0;
    d_above_frames = 0;
    d_last_above_frame = 0;
    d_segment_frames = 0;
    d_segment_peak = 0.0f;
    d_gap_until_frame = d_frames_processed;
}

void squelch_audio_sink::set_config(const squelch_audio_config &config)
{
    std::lock_guard<std::mutex> lock(d_mutex);
    if (d_state == STATE_ACTIVE)
        close_segment_locked(d_frames_processed, true);
    d_config = config;
    recompute_locked();
    reset_state_locked();
}

squelch_audio_config squelch_audio_sink::get_config() const
{
    std::lock_guard<std::mutex> lock(d_mutex);
    return d_config;
}

void squelch_audio_sink::set_center_frequency(double freq_hz)
{
    std::lock_guard<std::mutex> lock(d_mutex);
    d_config.center_freq_hz = freq_hz;
}

void squelch_audio_sink::set_trigger(const recording_trigger_sptr &trigger)
{
    std::lock_guard<std::mutex> lock(d_mutex);
    d_trigger = trigger;
}

void squelch_audio_sink::set_enabled(bool enabled)
{
    std::lock_guard<std::mutex> lock(d_mutex);
    if (enabled == d_enabled)
        return;
    if (!enabled && d_state == STATE_ACTIVE)
        close_segment_locked(d_frames_processed, true);
    d_enabled = enabled && d_config.sample_rate_hz > 0.0 && !d_config.directory.empty();
    reset_state_locked();
    if (d_enabled && !QDir().mkpath(QString::fromStdString(d_config.directory)))
    {
        std::cerr << "squelch_audio_sink: cannot create " << d_config.directory << std::endl;
        d_enabled = false;
    }
}

bool squelch_audio_sink::is_enabled() const
{
    std::lock_guard<std::mutex> lock(d_mutex);
    return d_enabled;
}

void squelch_audio_sink::push_ring_locked(float left, float right)
{
    if (d_ring.empty())
        return;
    d_ring[d_ring_pos] = frame_t {left, right};
    d_ring_pos = (d_ring_pos + 1) % d_ring.size();
    if (d_ring_count < d_ring.size())
        d_ring_count++;
}

void squelch_audio_sink::write_wav_header_locked(uint32_t data_bytes)
{
    char header[44];
    std::memset(header, 0, sizeof(header));
    std::memcpy(header, "RIFF", 4);
    put_u32(header + 4, 36 + data_bytes);
    std::memcpy(header + 8, "WAVEfmt ", 8);
    put_u32(header + 16, 16);
    put_u16(header + 20, 1);
    put_u16(header + 22, 2);
    put_u32(header + 24, static_cast<uint32_t>(std::llround(d_config.sample_rate_hz)));
    put_u32(header + 28, static_cast<uint32_t>(std::llround(d_config.sample_rate_hz)) * 4);
    put_u16(header + 32, 4);
    put_u16(header + 34, 16);
    std::memcpy(header + 36, "data", 4);
    put_u32(header + 40, data_bytes);
    d_stream.seekp(0);
    d_stream.write(header, sizeof(header));
    d_stream.seekp(0, std::ios::end);
}

void squelch_audio_sink::write_block_locked(const float *left, const float *right, int count)
{
    if (!d_stream.is_open() || count <= 0)
        return;
    std::vector<int16_t> pcm(static_cast<size_t>(count) * 2);
    for (int i = 0; i < count; i++)
    {
        const float l = std::max(-1.0f, std::min(1.0f, left[i]));
        const float r = std::max(-1.0f, std::min(1.0f, right[i]));
        pcm[2 * i] = static_cast<int16_t>(std::lrintf(l * 32767.0f));
        pcm[2 * i + 1] = static_cast<int16_t>(std::lrintf(r * 32767.0f));
    }
    const std::streamsize bytes = static_cast<std::streamsize>(pcm.size() * sizeof(int16_t));
    d_stream.write(reinterpret_cast<const char *>(pcm.data()), bytes);
    d_segment_frames += count;
    d_stats.frames += static_cast<uint64_t>(count);
    d_stats.bytes += static_cast<uint64_t>(bytes);
}

void squelch_audio_sink::write_ring_locked()
{
    if (d_ring.empty() || d_ring_count == 0 || !d_stream.is_open())
        return;
    const size_t start = (d_ring_pos + d_ring.size() - d_ring_count) % d_ring.size();
    std::vector<float> left(d_ring_count), right(d_ring_count);
    for (size_t i = 0; i < d_ring_count; i++)
    {
        const frame_t &frame = d_ring[(start + i) % d_ring.size()];
        left[i] = frame.left;
        right[i] = frame.right;
    }
    write_block_locked(left.data(), right.data(), static_cast<int>(d_ring_count));
    d_ring_count = 0;
    d_ring_pos = 0;
}

std::string squelch_audio_sink::make_filename_locked()
{
    const QString stamp = QDateTime::currentDateTimeUtc().toString("yyyyMMdd_HHmmss_zzz");
    const QString name = QString("%1_%2_%3_%4_%5.wav")
        .arg(QString::fromStdString(d_config.prefix))
        .arg(stamp)
        .arg(static_cast<qlonglong>(std::llround(d_config.center_freq_hz)))
        .arg(static_cast<qlonglong>(std::llround(d_config.sample_rate_hz)))
        .arg(++d_segment_seq);
    return QDir(QString::fromStdString(d_config.directory)).filePath(name).toStdString();
}

bool squelch_audio_sink::open_segment_locked(float peak_amplitude, bool with_ring)
{
    if (!QDir().mkpath(QString::fromStdString(d_config.directory)))
        return false;
    d_current_path = make_filename_locked();
    d_stream.open(d_current_path, std::ios::binary | std::ios::trunc);
    if (!d_stream.is_open())
    {
        std::cerr << "squelch_audio_sink: cannot open " << d_current_path << std::endl;
        d_current_path.clear();
        return false;
    }
    write_wav_header_locked(0);
    const double pre_roll_s = d_config.sample_rate_hz > 0.0
        ? static_cast<double>(d_ring_count) / d_config.sample_rate_hz : 0.0;
    const QDateTime start = QDateTime::currentDateTimeUtc()
        .addMSecs(static_cast<qint64>(-1000.0 * pre_roll_s));
    d_segment_start_iso = start.toString(Qt::ISODateWithMs).toStdString();
    d_segment_frames = 0;
    d_segment_peak = peak_amplitude;
    d_stats.started++;
    d_stats.last_file = d_current_path;
    if (with_ring)
        write_ring_locked();
    d_last_flush = std::chrono::steady_clock::now();
    return true;
}

void squelch_audio_sink::close_segment_locked(int64_t global_frame, bool keep)
{
    const uint32_t data_bytes = static_cast<uint32_t>(d_segment_frames * 4);
    if (d_stream.is_open())
    {
        write_wav_header_locked(data_bytes);
        d_stream.flush();
        d_stream.close();
    }
    const bool kept = keep && d_segment_frames >= d_min_event_frames && d_segment_frames > 0;
    if (kept)
    {
        d_stats.segments++;
        d_stats.last_file = d_current_path;
        write_index_locked(true);
    }
    else
    {
        d_stats.dropped++;
        if (!d_current_path.empty())
            QFile::remove(QString::fromStdString(d_current_path));
    }
    d_current_path.clear();
    d_segment_start_iso.clear();
    d_segment_frames = 0;
    d_segment_peak = 0.0f;
    d_state = STATE_IDLE;
    d_above_frames = 0;
    d_ring_count = 0;
    d_ring_pos = 0;
    d_gap_until_frame = global_frame + d_min_gap_frames;
}

void squelch_audio_sink::write_index_locked(bool kept)
{
    (void)kept;
    QFile file(QDir(QString::fromStdString(d_config.directory)).filePath("recordings.jsonl"));
    if (!file.open(QIODevice::Append | QIODevice::Text))
        return;
    const QJsonObject record {
        {"kind", "recording_audio"},
        {"file", QFileInfo(QString::fromStdString(d_current_path)).fileName()},
        {"frames", static_cast<qint64>(d_segment_frames)},
        {"duration_s", d_config.sample_rate_hz > 0.0
            ? static_cast<double>(d_segment_frames) / d_config.sample_rate_hz : 0.0},
        {"rate_hz", d_config.sample_rate_hz},
        {"freq_hz", d_config.center_freq_hz},
        {"start", QString::fromStdString(d_segment_start_iso)},
        {"peak_db", 20.0 * std::log10(static_cast<double>(d_segment_peak) + 1.0e-12)},
        {"threshold_db", d_config.threshold_dbfs},
        {"pre_roll_ms", d_config.pre_roll_ms},
        {"hangover_ms", d_config.hangover_ms},
        {"format", "wav_pcm16"},
    };
    file.write(QJsonDocument(record).toJson(QJsonDocument::Compact));
    file.write("\n");
    file.close();
}

int squelch_audio_sink::work(int noutput_items,
                            gr_vector_const_void_star &input_items,
                            gr_vector_void_star &output_items)
{
    (void)output_items;
    std::lock_guard<std::mutex> lock(d_mutex);
    if (!d_enabled || noutput_items <= 0)
        return noutput_items;
    if (d_config.max_total_bytes > 0 && d_stats.bytes >= d_config.max_total_bytes)
    {
        if (d_state == STATE_ACTIVE)
            close_segment_locked(d_frames_processed, true);
        d_enabled = false;
        return noutput_items;
    }

    const float *left = static_cast<const float *>(input_items[0]);
    const float *right = static_cast<const float *>(input_items[1]);
    int write_start = 0;

    for (int i = 0; i < noutput_items; i++)
    {
        const float amplitude = std::max(std::abs(left[i]), std::abs(right[i]));
        const int64_t global = d_frames_processed + i;
        if (d_state == STATE_IDLE)
        {
            push_ring_locked(left[i], right[i]);
            if (amplitude >= d_threshold_amplitude)
                d_above_frames++;
            else
                d_above_frames = 0;
            const bool shared_event = d_trigger
                && d_trigger->sequence.load() != d_last_trigger;
            if (shared_event || (d_above_frames >= d_trigger_frames && global >= d_gap_until_frame))
            {
                if (shared_event)
                    d_last_trigger = d_trigger->sequence.load();
                if (open_segment_locked(amplitude, true))
                {
                    d_state = STATE_ACTIVE;
                    d_last_above_frame = global;
                    d_above_frames = 0;
                    write_start = i + 1;
                }
                else
                {
                    d_above_frames = 0;
                }
            }
        }
        else
        {
            if (amplitude > d_segment_peak)
                d_segment_peak = amplitude;
            if (amplitude >= d_threshold_amplitude)
                d_last_above_frame = global;

            const int64_t pending = static_cast<int64_t>(i + 1 - write_start);
            const bool max_hit = d_max_segment_frames > 0
                && d_segment_frames + pending >= d_max_segment_frames;
            const bool shared_closed = d_trigger && !d_trigger->active.load();
            const bool hang = shared_closed
                ? (global - d_last_above_frame > d_hangover_frames)
                : (global - d_last_above_frame > d_hangover_frames);
            if (max_hit || hang)
            {
                write_block_locked(left + write_start, right + write_start, i + 1 - write_start);
                write_start = i + 1;
                const bool still_above = amplitude >= d_threshold_amplitude;
                close_segment_locked(global, true);
                if (max_hit && still_above && open_segment_locked(amplitude, false))
                {
                    d_state = STATE_ACTIVE;
                    d_last_above_frame = global;
                }
            }
        }
    }
    if (d_state == STATE_ACTIVE && write_start < noutput_items)
        write_block_locked(left + write_start, right + write_start, noutput_items - write_start);

    d_frames_processed += noutput_items;
    const auto now = std::chrono::steady_clock::now();
    if (d_stream.is_open()
        && std::chrono::duration_cast<std::chrono::seconds>(now - d_last_flush).count() >= 1)
    {
        d_stream.flush();
        d_last_flush = now;
    }
    return noutput_items;
}

void squelch_audio_sink::flush()
{
    std::lock_guard<std::mutex> lock(d_mutex);
    if (d_stream.is_open())
        d_stream.flush();
}

squelch_audio_stats squelch_audio_sink::get_stats() const
{
    std::lock_guard<std::mutex> lock(d_mutex);
    squelch_audio_stats stats = d_stats;
    stats.enabled = d_enabled;
    stats.active = (d_state == STATE_ACTIVE);
    if (!d_current_path.empty())
        stats.last_file = d_current_path;
    return stats;
}
