#include <QCoreApplication>
#include <QDir>
#include <QFile>
#include <QFileInfo>
#include <QJsonDocument>
#include <QJsonObject>
#include <QDateTime>

#include <gnuradio/io_signature.h>

#include <cmath>
#include <iostream>
#include <vector>

#include "dsp/squelch_iq_sink.h"

namespace {

struct TempDir
{
    QString path;
    TempDir()
    {
        static int counter = 0;
        path = QDir::tempPath() + "/gqrx-sink-"
             + QString::number(QCoreApplication::applicationPid()) + "-"
             + QString::number(++counter);
        QDir().mkpath(path);
    }
    ~TempDir() { QDir(path).removeRecursively(); }
};

void feed(const squelch_iq_sink_sptr &sink, const std::vector<gr_complex> &samples, int block = 17)
{
    gr_vector_const_void_star input(1);
    gr_vector_void_star output;
    for (size_t offset = 0; offset < samples.size(); offset += block)
    {
        const int count = static_cast<int>(std::min<size_t>(block, samples.size() - offset));
        input[0] = samples.data() + offset;
        sink->work(count, input, output);
    }
}

squelch_iq_config config(const QString &dir)
{
    squelch_iq_config cfg;
    cfg.directory = dir.toStdString();
    cfg.prefix = "test_evt";
    cfg.sample_rate_hz = 1000.0;
    cfg.center_freq_hz = 100000000.0;
    cfg.threshold_db = -30.0f;
    cfg.trigger_ms = 1.0;
    cfg.pre_roll_ms = 5.0;
    cfg.hangover_ms = 10.0;
    cfg.min_event_ms = 2.0;
    cfg.max_segment_s = 0.1;
    cfg.min_gap_ms = 0.0;
    return cfg;
}

std::vector<gr_complex> low(int count)
{
    return std::vector<gr_complex>(count, gr_complex(0.001f, -0.001f));
}

std::vector<gr_complex> high(int count)
{
    return std::vector<gr_complex>(count, gr_complex(0.1f, 0.0f));
}

QStringList files(const QString &dir, const QString &suffix)
{
    QDir d(dir);
    return d.entryList(QStringList() << QString("*%1").arg(suffix), QDir::Files, QDir::Name);
}

int count_files(const QString &dir, const QString &suffix)
{
    return files(dir, suffix).size();
}

} // namespace

int main(int argc, char **argv)
{
    QCoreApplication app(argc, argv);
    int failures = 0;

    {
        TempDir temp;
        const QString dir = temp.path + "/evidence";
        auto sink = make_squelch_iq_sink();
        sink->set_config(config(dir));
        sink->set_enabled(true);
        auto samples = low(50);
        auto burst = high(20);
        auto tail = low(50);
        samples.insert(samples.end(), burst.begin(), burst.end());
        samples.insert(samples.end(), tail.begin(), tail.end());
        feed(sink, samples);
        sink->set_enabled(false);
        const auto stats = sink->get_stats();
        if (stats.segments < 1 || count_files(dir, ".raw") < 1
            || !QFileInfo::exists(dir + "/recordings.jsonl")
            || stats.bytes == 0 || stats.samples == 0)
        {
            std::cerr << "FAIL: trigger/segment stats" << std::endl;
            failures++;
        }
    }

    {
        TempDir temp;
        const QString dir = temp.path + "/evidence";
        auto sink = make_squelch_iq_sink();
        auto cfg = config(dir);
        cfg.min_event_ms = 20.0;
        sink->set_config(cfg);
        sink->set_enabled(true);
        auto samples = low(50);
        auto burst = high(1);
        auto tail = low(50);
        samples.insert(samples.end(), burst.begin(), burst.end());
        samples.insert(samples.end(), tail.begin(), tail.end());
        feed(sink, samples);
        sink->set_enabled(false);
        const auto stats = sink->get_stats();
        if (stats.dropped < 1 || count_files(dir, ".raw") != 0)
        {
            std::cerr << "FAIL: min_event did not discard spike" << std::endl;
            failures++;
        }
    }

    {
        TempDir temp;
        const QString dir = temp.path + "/evidence";
        auto sink = make_squelch_iq_sink();
        sink->set_config(config(dir));
        sink->set_enabled(true);
        feed(sink, high(300));
        sink->set_enabled(false);
        const auto stats = sink->get_stats();
        if (stats.segments < 3 || count_files(dir, ".raw") < 3)
        {
            std::cerr << "FAIL: max_segment did not split" << std::endl;
            failures++;
        }
    }

    {
        TempDir temp;
        const QString dir = temp.path + "/evidence";
        auto sink = make_squelch_iq_sink();
        sink->set_config(config(dir));
        sink->set_enabled(false);
        feed(sink, high(300));
        if (count_files(dir, ".raw") != 0)
        {
            std::cerr << "FAIL: disabled sink wrote files" << std::endl;
            failures++;
        }
    }

    {
        TempDir temp;
        const QString dir = temp.path + "/evidence";
        auto sink = make_squelch_iq_sink();
        auto cfg = config(dir);
        cfg.max_total_bytes = 1000;
        cfg.max_segment_s = 0.05;
        sink->set_config(cfg);
        sink->set_enabled(true);
        feed(sink, high(300));
        if (sink->get_stats().enabled)
        {
            std::cerr << "FAIL: byte budget did not disarm sink" << std::endl;
            failures++;
        }
    }

    {
        TempDir temp;
        const QString dir = temp.path + "/evidence";
        auto sink = make_squelch_iq_sink();
        auto cfg = config(dir);
        cfg.sigmf = true;
        sink->set_config(cfg);
        sink->set_enabled(true);
        auto samples = low(20);
        auto burst = high(20);
        auto tail = low(20);
        samples.insert(samples.end(), burst.begin(), burst.end());
        samples.insert(samples.end(), tail.begin(), tail.end());
        feed(sink, samples);
        sink->set_enabled(false);
        if (count_files(dir, ".sigmf-data") < 1 || count_files(dir, ".sigmf-meta") < 1)
        {
            std::cerr << "FAIL: SigMF files missing" << std::endl;
            failures++;
        }
        else
        {
            QFile meta(dir + "/" + files(dir, ".sigmf-meta").first());
            if (!meta.open(QIODevice::ReadOnly))
            {
                std::cerr << "FAIL: cannot read SigMF meta" << std::endl;
                failures++;
            }
            else
            {
                const QJsonDocument doc = QJsonDocument::fromJson(meta.readAll());
                if (!doc.isObject() || !doc.object().contains("global"))
                {
                    std::cerr << "FAIL: invalid SigMF meta" << std::endl;
                    failures++;
                }
            }
        }
    }

    if (failures == 0)
        std::cout << "squelch_iq_sink tests passed" << std::endl;
    return failures == 0 ? 0 : 1;
}
