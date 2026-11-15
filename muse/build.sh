#!/usr/bin/env bash
set -euo pipefail
SUMI_ROOT="$(cd "$(dirname "$0")" && pwd)"
SUMI_PYTHON="${SUMI_PYTHON:-$SUMI_ROOT/.cache/host-venv/bin/python}"
export SUMI_BUILD_DIR="${SUMI_BUILD_DIR:-$SUMI_ROOT/build}"
export SUMI_DIST_DIR="${SUMI_DIST_DIR:-$SUMI_ROOT/dist}"
if [[ "${SUMI_WORLD_BENCH:-0}" == 1 && ( "$SUMI_BUILD_DIR" == "$SUMI_ROOT/build" || "$SUMI_DIST_DIR" == "$SUMI_ROOT/dist" ) ]]; then
    echo 'Diagnostic builds require separate SUMI_BUILD_DIR and SUMI_DIST_DIR.' >&2
    exit 1
fi
if [[ ! -x "$SUMI_PYTHON" ]]; then
    echo 'Prepare Python 3.10–3.13, Pillow, CMake and Ninja; see muse/README.md.' >&2
    exit 1
fi
export PATH="$(dirname "$SUMI_PYTHON"):$PATH"
export IDF_TOOLS_PATH="${IDF_TOOLS_PATH:-$SUMI_ROOT/.cache/idf-tools}"
export IDF_PATH="${IDF_PATH:-$SUMI_ROOT/.cache/esp-idf-v6.0.3}"
if [[ ! -f "$IDF_PATH/export.sh" ]]; then
    echo 'ESP-IDF 6.0.3 is required; see muse/README.md.' >&2
    exit 1
fi
"$SUMI_PYTHON" "$SUMI_ROOT/prepare.py"
# IDF's export script is not safe with nounset in every supported shell.
set +u
source "$IDF_PATH/export.sh"
set -u
idf.py --version | grep -q '6\.0\.3' || { echo 'Use ESP-IDF 6.0.3.' >&2; exit 1; }
SUMI_CONFIGS='sdkconfig.defaults;devices/sdkconfig.muse;devices/sdkconfig.muse-m5stack-stopwatch;sdkconfig.sumi'
if [[ "${SUMI_WORLD_BENCH:-0}" == 1 ]]; then
    SUMI_CONFIGS="$SUMI_CONFIGS;$SUMI_ROOT/voice_training/native/esp32/sdkconfig.world-bench"
fi
idf.py -C "$SUMI_ROOT/.cache/muse-gadget-sdk-463bd2a05380fea4211b0099ad0f70933c108b20/esp32" \
    -B "$SUMI_BUILD_DIR" -DIDF_TARGET=esp32s3 \
    "-DSDKCONFIG_DEFAULTS=$SUMI_CONFIGS" build size
"$SUMI_PYTHON" "$SUMI_ROOT/package.py"
