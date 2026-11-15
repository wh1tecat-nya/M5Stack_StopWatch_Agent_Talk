#!/usr/bin/env bash
set -euo pipefail
root="$(cd "$(dirname "$0")" && pwd)"
output="${SUMI_ANDROID_BUILD_DIR:-$root/../.cache/android-ble}"
sdk="${ANDROID_SDK_ROOT:-${ANDROID_HOME:-$HOME/Library/Android/sdk}}"
platform="$sdk/platforms/android-36/android.jar"
tools="$sdk/build-tools/36.0.0"
mkdir -p "$output"
output="$(cd "$output" && pwd)"
mkdir -p "$output/classes" "$output/dex"
if [[ -n "${JAVA_HOME:-}" ]]; then export PATH="$JAVA_HOME/bin:$PATH"; fi
javac --release 17 -classpath "$platform" -d "$output/classes" "$root"/src/jp/sumi/stopwatch/*.java "$root"/src/jp/irodori/android/*.java
jar cf "$output/classes.jar" -C "$output/classes" .
"$tools/d8" --lib "$platform" --min-api 33 --output "$output/dex" "$output/classes.jar"
"$tools/aapt2" link --manifest "$root/AndroidManifest.xml" -I "$platform" -o "$output/unsigned.apk"
(cd "$output/dex" && zip -q "$output/unsigned.apk" classes.dex)
if [[ -n "${SUMI_IRODORI_APK:-}" ]]; then
    python3 - "$SUMI_IRODORI_APK" "$output/unsigned.apk" <<'PYTHON'
import hashlib
from pathlib import Path
import sys
from zipfile import ZipFile, ZIP_DEFLATED
apk = Path(sys.argv[1])
with apk.open('rb') as file:
    if hashlib.file_digest(file, 'sha256').hexdigest() != 'e535e51675899360a4950c70b2f1838ca726bfcd9b90a8a4d142a751cf644b8d':
        raise SystemExit('Expected verified Irodori Offline v0.45.0 APK')
libraries = {'lib/arm64-v8a/libirodori-jni.so', 'lib/arm64-v8a/libc++_shared.so'}
with ZipFile(apk) as source, ZipFile(sys.argv[2], 'a', compression=ZIP_DEFLATED) as target:
    for library in sorted(libraries):
        if library not in source.namelist() and library.endswith('/libc++_shared.so'):
            continue
        target.writestr(library, source.read(library))
PYTHON
fi
"$tools/zipalign" -fp 4 "$output/unsigned.apk" "$output/aligned.apk"
if [[ -n "${SUMI_ANDROID_KEYSTORE:-}" ]]; then
    "$tools/apksigner" sign --ks "$SUMI_ANDROID_KEYSTORE" --out "$output/sumi-stopwatch.apk" "$output/aligned.apk"
    "$tools/apksigner" verify --verbose "$output/sumi-stopwatch.apk"
else
    printf 'Unsigned APK: %s\n' "$output/aligned.apk"
fi
