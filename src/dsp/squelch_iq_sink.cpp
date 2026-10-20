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
#include "squelch_iq_sink.h"

#include <gnuradio/io_signature.h>

#include <QDateTime>
#include <QDir>
#include <QFile>
#include <QFileInfo>
#include <QJsonArray>
#include <QJsonDocument>
#include <QJsonObject>

#include <algorithm>
#include <cmath>
#include <cstring>
#include <iostream>
#include <sstream>

squelch_iq_sink_sptr make_squelch_iq_sink()
{
    return gnuradio::get_initial_sptr(new squelch_iq_sink());
}

squelch_iq_sink::squelch_iq_sink()
    : gr::sync_block("squelch_iq_sink",
                     gr::io_signature::make(1, 1, sizeof(gr_complex)),
                     gr::io_signature::make(0, 0, 0)),
      d_last_flush(std::chrono::steady_clock::now())
{
}

squelch_iq_sink::~squelch_iq_sink()
{
    std::lock_guard<std::mutex> lock(d_mutex);
    if (d_state == STATE_ACTIVE)
        close_segment_locked(d_samples_processed, true);
}

void squelch_iq_sink::recompute_locked()
{
    const double rate = d_config.sample_rate_hz;
    auto ms_to_samples = [rate](double ms) -> int64_t {
        if (rate <= 0.0 || ms <= 0.0)
            return 0;
        return static_cast<int64_t>(std::llround(rate * ms / 1000.0));
    };

    d_threshold_power = static_cast<float>(
        std::pow(10.0, static_cast<double>(d_config.threshold_db) / 10.0));
    d_trigger_samples = std::max<int64_t>(1, ms_to_samples(d_config.trigger_ms));
    d_pre_roll_samples = ms_to_samples(d_config.pre_roll_ms);
    d_hangover_samples = ms_to_samples(d_config.hangover_ms);
    d_min_event_samples = ms_to_samples(d_config.min_event_ms);
    d_max_segment_samples = ms_to_samples(d_config.max_segment_s * 1000.0);
    d_min_gap_samples = ms_to_samples(d_config.min_gap_ms);

    d_ring.assign(static_cast<size_t>(std::max<int64_t>(1, d_pre_roll_samples)),
                  gr_complex(0.0f, 0.0f));
}

void squelch_iq_sink::reset_state_locked()
{
    d_state = STATE_IDLE;
    d_ring_pos = 0;
    d_ring_count = 0;
    d_above_samples = 0;
    d_last_above_sample = 0;
    d_segment_samples = 0;
    d_segment_peak = 0.0f;
    d_gap_until_sample = d_samples_processed;
}

void squelch_iq_sink::set_config(const squelch_iq_config &config)
{
    std::lock_guard<std::mutex> lock(d_mutex);
    if (d_state == STATE_ACTIVE)
        close_segment_locked(d_samples_processed, true);
    d_config = config;
    recompute_locked();
    reset_state_locked();
}

squelch_iq_config squelch_iq_sink::get_config() const
{
    std::lock_guard<std::mutex> lock(d_mutex);
    return d_config;
}

void squelch_iq_sink::set_center_frequency(double freq_hz)
{
    std::lock_guard<std::mutex> lock(d_mutex);
    d_config.center_freq_hz = freq_hz;
}

void squelch_iq_sink::set_sample_rate(double rate_hz)
{
    std::lock_guard<std::mutex> lock(d_mutex);
    if (d_state == STATE_ACTIVE)
        close_segment_locked(d_samples_processed, true);
    d_config.sample_rate_hz = rate_hz;
    recompute_locked();
    reset_state_locked();
}

void squelch_iq_sink::set_enabled(bool enabled)
{
    std::lock_guard<std::mutex> lock(d_mutex);
    if (enabled == d_enabled)
        return;
    if (!enabled && d_state == STATE_ACTIVE)
        close_segment_locked(d_samples_processed, true);
    d_enabled = enabled && d_config.sample_rate_hz > 0.0 && !d_config.directory.empty();
    reset_state_locked();
    if (d_enabled)
    {
        if (!QDir().mkpath(QString::fromStdString(d_config.directory)))
        {
            std::cerr << "squelch_iq_sink: cannot create " << d_config.directory << std::endl;
            d_enabled = false;
        }
    }
}

bool squelch_iq_sink::is_enabled() const
{
    std::lock_guard<std::mutex> lock(d_mutex);
    return d_enabled;
}

void squelch_iq_sink::push_ring_locked(const gr_complex &sample)
{
    if (d_ring.empty())
        return;
    d_ring[d_ring_pos] = sample;
    d_ring_pos = (d_ring_pos + 1) % d_ring.size();
    if (d_ring_count < d_ring.size())
        d_ring_count++;
}

void squelch_iq_sink::write_block_locked(const gr_complex *samples, int count)
{
    if (!d_stream.is_open() || count <= 0)
        return;
    const std::streamsize bytes = static_cast<std::streamsize>(count) * sizeof(gr_complex);
    d_stream.write(reinterpret_cast<const char *>(samples), bytes);
    d_segment_samples += count;
    d_stats.samples += static_cast<uint64_t>(count);
    d_stats.bytes += static_cast<uint64_t>(bytes);
}

void squelch_iq_sink::write_ring_locked()
{
    if (d_ring.empty() || d_ring_count == 0 || !d_stream.is_open())
        return;
    const size_t start = (d_ring_pos + d_ring.size() - d_ring_count) % d_ring.size();
    if (start + d_ring_count <= d_ring.size())
    {
        write_block_locked(&d_ring[start], static_cast<int>(d_ring_count));
    }
    else
    {
        const size_t first = d_ring.size() - start;
        write_block_locked(&d_ring[start], static_cast<int>(first));
        write_block_locked(&d_ring[0], static_cast<int>(d_ring_count - first));
    }
    d_ring_count = 0;
    d_ring_pos = 0;
}

std::string squelch_iq_sink::make_filename_locked(const std::string &suffix)
{
    const QDateTime now = QDateTime::currentDateTimeUtc();
    const QString stamp = now.toString("yyyyMMdd_HHmmss_zzz");
    const QString prefix = QString::fromStdString(d_config.prefix);
    const QString name = QString("%1_%2_%3_%4_%5_fc.%6")
        .arg(prefix)
        .arg(stamp)
        .arg(static_cast<qlonglong>(std::llround(d_config.center_freq_hz)))
        .arg(static_cast<qlonglong>(std::llround(d_config.sample_rate_hz)))
        .arg(++d_segment_seq)
        .arg(QString::fromStdString(suffix));
    return QDir(QString::fromStdString(d_config.directory)).filePath(name).toStdString();
}

bool squelch_iq_sink::open_segment_locked(int64_t global_sample, float peak_power, bool with_ring)
{
    (void)global_sample;
    const QDateTime now = QDateTime::currentDateTimeUtc();
    const double rate = d_config.sample_rate_hz;
    const double pre_roll_s = rate > 0.0 ? static_cast<double>(d_ring_count) / rate : 0.0;
    const QDateTime start = now.addMSecs(static_cast<qint64>(-1000.0 * pre_roll_s));
    d_segment_start_iso = start.toString(Qt::ISODateWithMs).toStdString();

    if (!QDir().mkpath(QString::fromStdString(d_config.directory)))
        return false;

    d_current_path = make_filename_locked(d_config.sigmf ? "sigmf-data" : "raw");
    if (d_config.sigmf)
        d_current_meta = d_current_path.substr(0, d_current_path.size() - 11) + ".sigmf-meta";
    else
        d_current_meta.clear();

    d_stream.open(d_current_path, std::ios::binary | std::ios::trunc);
    if (!d_stream.is_open())
    {
        std::cerr << "squelch_iq_sink: cannot open " << d_current_path << std::endl;
        d_current_path.clear();
        d_current_meta.clear();
        return false;
    }

    d_segment_samples = 0;
    d_segment_peak = peak_power;
    d_stats.started++;
    d_stats.last_file = d_current_path;
    if (with_ring)
        write_ring_locked();
    d_last_flush = std::chrono::steady_clock::now();
    return true;
}

void squelch_iq_sink::close_segment_locked(int64_t global_sample, bool keep)
{
    if (d_stream.is_open())
    {
        d_stream.flush();
        d_stream.close();
    }

    const bool long_enough = d_segment_samples >= d_min_event_samples && d_segment_samples > 0;
    const bool kept = keep && long_enough;
    if (kept)
    {
        d_stats.segments++;
        d_stats.last_file = d_current_path;
        write_index_locked(true);
        write_sigmf_meta_locked(true);
    }
    else
    {
        d_stats.dropped++;
        if (!d_current_path.empty())
            QFile::remove(QString::fromStdString(d_current_path));
        if (!d_current_meta.empty())
            QFile::remove(QString::fromStdString(d_current_meta));
    }

    d_current_path.clear();
    d_current_meta.clear();
    d_segment_start_iso.clear();
    d_segment_samples = 0;
    d_segment_peak = 0.0f;
    d_state = STATE_IDLE;
    d_above_samples = 0;
    d_ring_count = 0;
    d_ring_pos = 0;
    d_gap_until_sample = global_sample + d_min_gap_samples;
}

void squelch_iq_sink::write_index_locked(bool kept)
{
    (void)kept;
    const QString path = QDir(QString::fromStdString(d_config.directory)).filePath("evidence.jsonl");
    QFile file(path);
    if (!file.open(QIODevice::Append | QIODevice::Text))
        return;
    const double peak_db = 10.0 * std::log10(static_cast<double>(d_segment_peak) + 1.0e-20);
    const double duration = d_config.sample_rate_hz > 0.0
        ? static_cast<double>(d_segment_samples) / d_config.sample_rate_hz : 0.0;
    QJsonObject record {
        {"kind", "evidence_iq"},
        {"file", QFileInfo(QString::fromStdString(d_current_path)).fileName()},
        {"samples", static_cast<qint64>(d_segment_samples)},
        {"duration_s", duration},
        {"rate_hz", d_config.sample_rate_hz},
        {"freq_hz", d_config.center_freq_hz},
        {"start", QString::fromStdString(d_segment_start_iso)},
        {"peak_db", peak_db},
        {"threshold_db", d_config.threshold_db},
        {"pre_roll_ms", d_config.pre_roll_ms},
        {"hangover_ms", d_config.hangover_ms},
        {"format", d_config.sigmf ? "sigmf" : "cf32"},
    };
    if (!d_current_meta.empty())
        record["meta"] = QFileInfo(QString::fromStdString(d_current_meta)).fileName();
    file.write(QJsonDocument(record).toJson(QJsonDocument::Compact));
    file.write("\n");
    file.close();
}

void squelch_iq_sink::write_sigmf_meta_locked(bool kept)
{
    if (!d_config.sigmf || d_current_meta.empty())
        return;
    if (!kept)
    {
        QFile::remove(QString::fromStdString(d_current_meta));
        return;
    }
    QFile file(QString::fromStdString(d_current_meta));
    if (!file.open(QIODevice::WriteOnly | QIODevice::Truncate))
        return;
    const QJsonObject global {
#if Q_BYTE_ORDER == Q_BIG_ENDIAN
        {"core:datatype", "cf32_be"},
#else
        {"core:datatype", "cf32_le"},
#endif
        {"core:sample_rate", d_config.sample_rate_hz},
        {"core:version", "1.0.0"},
        {"core:recorder", "Gqrx squelch_iq_sink"},
        {"core:description", "Squelch-gated I/Q evidence segment"},
    };
    const QJsonObject capture {
        {"core:sample_start", 0},
        {"core:frequency", d_config.center_freq_hz},
        {"core:datetime", QString::fromStdString(d_segment_start_iso)},
    };
    const QJsonObject annotation {
        {"core:sample_start", 0},
        {"core:sample_count", static_cast<qint64>(d_segment_samples)},
        {"core:comment", QString("trigger=%1 dBFS peak=%2 dBFS pre_roll=%3 ms hangover=%4 ms")
                            .arg(static_cast<double>(d_config.threshold_db), 0, 'f', 1)
                            .arg(10.0 * std::log10(static_cast<double>(d_segment_peak) + 1.0e-20), 0, 'f', 1)
                            .arg(d_config.pre_roll_ms, 0, 'f', 0)
                            .arg(d_config.hangover_ms, 0, 'f', 0)},
    };
    const QJsonObject root {
        {"global", global},
        {"captures", QJsonArray { capture }},
        {"annotations", QJsonArray { annotation }},
    };
    file.write(QJsonDocument(root).toJson(QJsonDocument::Indented));
    file.close();
}

int squelch_iq_sink::work(int noutput_items,
                          gr_vector_const_void_star &input_items,
                          gr_vector_void_star &output_items)
{
    (void)output_items;
    std::lock_guard<std::mutex> lock(d_mutex);
    if (!d_enabled || noutput_items <= 0)
        return noutput_items;

    const gr_complex *in = static_cast<const gr_complex *>(input_items[0]);
    int write_start = 0;

    for (int i = 0; i < noutput_items; i++)
    {
        const float power = std::norm(in[i]);
        const int64_t global = d_samples_processed + i;

        if (d_state == STATE_IDLE)
        {
            push_ring_locked(in[i]);
            if (power >= d_threshold_power)
                d_above_samples++;
            else
                d_above_samples = 0;

            if (d_above_samples >= d_trigger_samples && global >= d_gap_until_sample)
            {
                if (open_segment_locked(global, power, true))
                {
                    d_state = STATE_ACTIVE;
                    d_last_above_sample = global;
                    d_above_samples = 0;
                    write_start = i + 1;
                }
                else
                {
                    d_above_samples = 0;
                }
            }
        }
        else
        {
            if (power > d_segment_peak)
                d_segment_peak = power;
            if (power >= d_threshold_power)
                d_last_above_sample = global;

            const int64_t pending = static_cast<int64_t>(i + 1 - write_start);
            const bool max_hit = d_max_segment_samples > 0
                && d_segment_samples + pending >= d_max_segment_samples;
            const bool hang = global - d_last_above_sample > d_hangover_samples;

            if (max_hit || hang)
            {
                write_block_locked(in + write_start, i + 1 - write_start);
                write_start = i + 1;
                const bool still_above = power >= d_threshold_power;
                close_segment_locked(global, true);
                if (max_hit && still_above && open_segment_locked(global, power, false))
                {
                    d_state = STATE_ACTIVE;
                    d_last_above_sample = global;
                }
            }
        }
    }

    if (d_state == STATE_ACTIVE && write_start < noutput_items)
        write_block_locked(in + write_start, noutput_items - write_start);

    d_samples_processed += noutput_items;

    const auto now = std::chrono::steady_clock::now();
    if (d_stream.is_open()
        && std::chrono::duration_cast<std::chrono::seconds>(now - d_last_flush).count() >= 1)
    {
        d_stream.flush();
        d_last_flush = now;
    }

    return noutput_items;
}

void squelch_iq_sink::flush()
{
    std::lock_guard<std::mutex> lock(d_mutex);
    if (d_stream.is_open())
        d_stream.flush();
}

squelch_iq_stats squelch_iq_sink::get_stats() const
{
    std::lock_guard<std::mutex> lock(d_mutex);
    squelch_iq_stats stats = d_stats;
    stats.enabled = d_enabled;
    stats.active = (d_state == STATE_ACTIVE);
    if (!d_current_path.empty())
        stats.last_file = d_current_path;
    return stats;
}
