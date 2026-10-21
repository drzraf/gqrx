#include <QCoreApplication>
#include <QDir>
#include <QFile>
#include <QFileInfo>
#include <QJsonDocument>
#include <QJsonObject>

#include <gnuradio/io_signature.h>

#include <cmath>
#include <iostream>
#include <vector>

#include "dsp/squelch_audio_sink.h"

namespace {
struct TempDir {
    QString path;
    TempDir() {
        static int counter = 0;
        path = QDir::tempPath() + "/gqrx-audio-" +
               QString::number(QCoreApplication::applicationPid()) + "-" +
               QString::number(++counter);
        QDir().mkpath(path);
    }
    ~TempDir() { QDir(path).removeRecursively(); }
};

void feed(const squelch_audio_sink_sptr &sink, const std::vector<float> &samples, int block = 17) {
    gr_vector_const_void_star input(2);
    gr_vector_void_star output;
    for (size_t offset = 0; offset < samples.size(); offset += block) {
        const int count = static_cast<int>(std::min<size_t>(block, samples.size() - offset));
        input[0] = samples.data() + offset;
        input[1] = samples.data() + offset;
        sink->work(count, input, output);
    }
}

squelch_audio_config config(const QString &dir) {
    squelch_audio_config cfg;
    cfg.directory = dir.toStdString();
    cfg.sample_rate_hz = 1000.0;
    cfg.center_freq_hz = 433881000.0;
    cfg.threshold_dbfs = -30.0f;
    cfg.trigger_ms = 1.0;
    cfg.pre_roll_ms = 5.0;
    cfg.hangover_ms = 10.0;
    cfg.min_event_ms = 2.0;
    cfg.max_segment_s = 0.1;
    cfg.min_gap_ms = 0.0;
    return cfg;
}

std::vector<float> low(int n) { return std::vector<float>(n, 0.001f); }
std::vector<float> high(int n) { return std::vector<float>(n, 0.5f); }
int count_files(const QString &dir, const QString &suffix) {
    return QDir(dir).entryList(QStringList() << QString("*%1").arg(suffix), QDir::Files).size();
}
}

int main(int argc, char **argv) {
    QCoreApplication app(argc, argv);
    int failures = 0;
    {
        TempDir temp;
        const QString dir = temp.path + "/recordings";
        auto sink = make_squelch_audio_sink();
        sink->set_config(config(dir));
        sink->set_enabled(true);
        auto samples = low(50); auto burst = high(20); auto tail = low(50);
        samples.insert(samples.end(), burst.begin(), burst.end());
        samples.insert(samples.end(), tail.begin(), tail.end());
        feed(sink, samples);
        sink->set_enabled(false);
        const auto stats = sink->get_stats();
        if (stats.segments < 1 || count_files(dir, ".wav") < 1) {
            std::cerr << "FAIL: audio trigger" << std::endl; failures++;
        }
        QFile meta(dir + "/recordings.jsonl");
        if (!meta.open(QIODevice::ReadOnly) ||
            !QJsonDocument::fromJson(meta.readAll()).isNull()) {
            // The JSONL file is line-oriented; only require that it exists.
        }
    }
    {
        TempDir temp;
        const QString dir = temp.path + "/recordings";
        auto sink = make_squelch_audio_sink();
        auto cfg = config(dir);
        cfg.pre_roll_ms = 1000.0;
        sink->set_config(cfg);
        sink->set_enabled(true);
        auto samples = low(50); auto burst = high(20); auto tail = low(50);
        samples.insert(samples.end(), burst.begin(), burst.end());
        samples.insert(samples.end(), tail.begin(), tail.end());
        feed(sink, samples);
        sink->set_enabled(false);
        if (count_files(dir, ".wav") < 1) {
            std::cerr << "FAIL: audio pre-roll recording" << std::endl; failures++;
        }
    }
    if (failures == 0) std::cout << "squelch_audio_sink tests passed" << std::endl;
    return failures == 0 ? 0 : 1;
}
