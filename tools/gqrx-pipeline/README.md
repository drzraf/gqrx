# gqrx-pipeline (P0)

Dependency-light tooling for turning gqrx into a **generic signal-evidence and
decode pipeline**. It is not OOK-specific: frequency, mode, passband, squelch
and decoder are parameters. OOK is one acceptance fixture.

The pipeline deliberately produces **human evidence first**:

- screenshot of the spectrum/waterfall at the trigger;
- short demodulated audio clip;
- spectrogram PNG;
- metadata JSON;
- optional **bounded** IQ segment (off by default).

A human can confirm “there is a signal here” without opening a large IQ file.
The optional IQ segment is then used by rtl_433/URH for decoding.

## Files

| File | Purpose |
|---|---|
| `gqrxctl.py` | dependency-free gqrx TCP client |
| `session.py` | session directory, JSONL index, quota, retention |
| `scan.py` | FFT scan when `FFT Q`/`FFT V` is available; poll fallback |
| `monitor.py` | adaptive level trigger + evidence capture |
| `evidence.py` | evidence bundle builder |
| `screenshot.py` | screenshot-wrapper / grim / X11 capture |
| `decode.py` | thin rtl_433 wrapper for CF32/CU8/CS16 files |
| `tests/` | stdlib `unittest` tests; synthetic only, no long IQ |

## Requirements

- Python 3.9+ (stdlib only for the core).
- gqrx with **Tools → Remote control** enabled.
- Optional: `sox` or `ffmpeg` for spectrograms.
- Optional: `rtl_433` (or a built `rtl433_up`) for decoding.
- Optional: Wayland screenshots via
  `~/comp/dev/wayland-self-screenshot-any/screenshot-wrapper` (installed at
  `/usr/local/bin/screenshot-wrapper`).

## Quick start

### 1. Launch gqrx under the Wayland screenshot wrapper

The wrapper must launch the application; it cannot attach to an already-running
process.

```sh
mkdir -p /tmp/gqrx-evidence
SCREENSHOT_FILE=/tmp/gqrx-evidence/%T.jpg \
SCREENSHOT_TIMEOUT=off \
SCREENSHOT_PIDFILE=/tmp/gqrx-evidence/pid \
  /usr/local/bin/screenshot-wrapper gqrx -c default.conf
```

For X11, skip the wrapper and use `--screenshot-method x11`; for Wayland
whole-screen use `grim`.

### 2. Monitor a frequency and capture evidence

```sh
python3 tools/gqrx-pipeline/monitor.py \
  --freq 433900000 --mode AM --passband 20000 \
  --session-root ~/sdr/sessions \
  --screenshot-pidfile /tmp/gqrx-evidence/pid \
  --screenshot-dir /tmp/gqrx-evidence \
  --audio-dir ~/ \
  --audio-seconds 2 \
  --iq-seconds 0
```

Defaults: screenshot + audio + spectrogram; **no IQ**. To keep a bounded IQ
segment:

```sh
python3 tools/gqrx-pipeline/monitor.py \
  --freq 145500000 --mode FM --passband 15000 \
  --iq-seconds 1 --iq-rate 250000 --iq-bytes-per-sample 8 \
  --iq-max-mb 4 --iq-dir ~/sdr/iq
```

`8` bytes/sample = CF32 (gqrx master); `2` = CU8 (with PR #1013); `4` = CS16.

### 3. Scan without retuning per bin

If gqrx has PR #1458 (`FFT Q`/`FFT V`):

```sh
python3 tools/gqrx-pipeline/scan.py \
  --start 433000000 --end 435000000 \
  --mode fft --pool 25000 --min-prominence 6 \
  --output ~/sdr/sessions/scan.jsonl
```

Without PR #1458, `--mode auto` falls back to `F` + `l STRENGTH` polling
(`--mode poll`), which is slower and coarser.

### 4. Decode a recording

```sh
python3 tools/gqrx-pipeline/decode.py \
  --file /path/to/gqrx_..._fc.raw \
  --rate 250000 --freq 433900000 \
  --output ~/sdr/sessions/events.jsonl
```

Optional flex spec (OOK fixture only):

```sh
--flex 'n=fixture,m=OOK_PWM,s=467,l=927,r=2000,g=0,t=0,bits=29'
```

## Session layout

```text
~/sdr/sessions/<timestamp>/
  session.json
  index.jsonl
  scan.jsonl
  events.jsonl
  bundles/<timestamp>-<freq>/
    screenshot.jpg
    audio.wav
    spectrogram.png
    metadata.json
    iq.raw            # optional, bounded
```

`index.jsonl` is the machine-readable summary; `metadata.json` is the
human/forensic record inside each bundle.

## Storage and CPU rules

- **IQ is off by default.** A CF32 stream at 2 MSps is ~57.6 GB/hour.
- Per-trigger IQ is capped by `--iq-max-mb`; default 8 MB.
- Session quota defaults to 1 GB **per session** (rotate daily); retention defaults to 7 days.
- Tests use synthetic buffers; do not run long captures in tests.
- Scan with one FFT query per window, not one tune per bin.
- Decode/record at 250–500 kSps, not at the full source rate.
- Run URH/auto-detection offline on bursts/windows, not continuously.

## Live validation

`FFT Q` / `FFT V` were validated against a running gqrx built from `feature/gqrx-pipeline-integration`, using a small synthetic CF32 file source and the Gr-audio backend. `scan.py` parsed the response and found the synthetic +150 kHz tone at 433.9138 MHz. See `context/GQRX_PIPELINE.md` §9.5.

## Tests

```sh
python3 -m unittest discover -s tools/gqrx-pipeline/tests -v
```

No hardware, no remote connection, and no large IQ files are required.

## Commit provenance

Changes should be small and logical. Include the origin in every commit:

```text
Origin: original work | <owner>/<repo>#<PR> | <owner>/<repo>:<branch>
Conflict resolution: none | <files/notes>
Upstream-status: not-applicable | open | merged | closed
```

See `context/UPSTREAM_TRACKING.md` for the ledger.
