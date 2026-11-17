"""オプトインした診断ビルドだけに、固定モデルとローカル測定用入力を組み込む。"""
import hashlib
import json
import os
from pathlib import Path
import shutil

from prepare_world_source import prepare
from prepare_world_float import prepare_float
from prepare_esp_dsp import prepare_dsp


def prepare_device(esp, enabled, copy_changed, write_changed):
    component = esp / 'components/sumi_world_bench'
    cmake = component / 'CMakeLists.txt'
    if not enabled:
        if component.exists():
            write_changed(cmake, b'idf_component_register()\n')
        return
    here = Path(__file__).resolve().parent
    cache = here.parent / '.cache'
    fp32 = os.environ.get('SUMI_WORLD_FP32') == '1'
    dsp = os.environ.get('SUMI_WORLD_ESP_DSP') == '1'
    dsp_fft = os.environ.get('SUMI_WORLD_FFT_DSP') == '1'
    bounded_math = os.environ.get('SUMI_WORLD_BOUNDED_MATH') == '1'
    math_profile = os.environ.get('SUMI_WORLD_MATH_PROFILE') == '1'
    lut_math = os.environ.get('SUMI_WORLD_LUT_MATH') == '1'
    full_wave = os.environ.get('SUMI_WORLD_BENCH_FULL_WAVE') == '1'
    hot_code = os.environ.get('SUMI_WORLD_HOT_CODE') == '1'
    compact = os.environ.get('SUMI_WORLD_COMPACT_WORKSPACE') == '1'
    internal_stack = os.environ.get('SUMI_WORLD_BENCH_INTERNAL_STACK') == '1'
    phase_recurrence = os.environ.get('SUMI_WORLD_PHASE_RECURRENCE') == '1'
    silu_table = os.environ.get('SUMI_WORLD_SILU_TABLE') == '1'
    pipeline = os.environ.get('SUMI_WORLD_BENCH_PIPELINE') == '1'
    playback = os.environ.get('SUMI_WORLD_BENCH_PLAYBACK') == '1'
    if playback and not pipeline:
        raise ValueError('Speaker diagnostic requires pipeline')
    if compact and not dsp_fft:
        raise ValueError('Compact workspace requires DSP FFT')
    if internal_stack and not compact:
        raise ValueError('Internal-stack experiment requires compact workspace')
    if pipeline and not (internal_stack and fp32 and full_wave):
        raise ValueError('Pipeline diagnostic requires internal stack, FP32 and full wave')
    wave_priority = int(os.environ.get('SUMI_WORLD_BENCH_WAVE_PRIORITY', '3'))
    if wave_priority not in (3, 6):
        raise ValueError('Unsupported waveform task priority')
    stack_bytes = int(os.environ.get('SUMI_WORLD_BENCH_STACK_BYTES', '12288'))
    tile_frames = int(os.environ.get('SUMI_WORLD_MATRIX_TILE_FRAMES', '4' if compact else '8'))
    filter_stride = int(os.environ.get('SUMI_WORLD_FILTER_STRIDE', '0'))
    if filter_stride not in (0, 1, 2) or (filter_stride and not (fp32 and dsp_fft and lut_math)):
        raise ValueError('Experimental frame filters require FP32, DSP FFT and LUT math')
    if stack_bytes not in (4096, 8192, 12288) or tile_frames not in (4, 8):
        raise ValueError('Unsupported stack or matrix workspace size')
    if hot_code and not dsp_fft:
        raise ValueError('Hot-code placement requires DSP FFT')
    fft_size = int(os.environ.get('SUMI_WORLD_SYNTH_FFT_SIZE', '1024'))
    sample_rate = int(os.environ.get('SUMI_WORLD_SAMPLE_RATE', '22050'))
    if sample_rate not in (16000, 22050) or (sample_rate != 22050 and not (fp32 and dsp_fft and full_wave)):
        raise ValueError('16 kHz diagnostic requires FP32 DSP FFT and full-wave measurement')
    if fft_size not in (256, 512, 1024) or (fft_size != 1024 and not (fp32 and dsp_fft)) or (fft_size == 256 and sample_rate != 16000):
        raise ValueError('Experimental synthesis size requires FP32 DSP FFT')
    chunk = int(os.environ.get('SUMI_WORLD_BENCH_CHUNK', '64'))
    if chunk not in (64, 128, 256, 384, 512):
        raise ValueError('Unsupported benchmark chunk size')
    if (bounded_math or math_profile or lut_math) and not fp32:
        raise ValueError('Math experiments require FP32')
    if dsp_fft and not (fp32 and dsp):
        raise ValueError('DSP FFT requires FP32 and ESP-DSP')
    source = prepare_float(bounded_math, math_profile, lut_math) if fp32 else prepare()
    for path in (source / 'src').rglob('*'):
        if path.is_file():
            copy_changed(path, component / 'world' / path.relative_to(source / 'src'))
    copy_changed(source / 'LICENSE.txt', component / 'WORLD-LICENSE.txt')
    for name in ('world_envelope.cpp', 'world_envelope.h', 'world_stream.cpp', 'world_stream.h', 'world_fft_dsp.cpp', 'world_fft_dsp.h', 'world_fast_math.h', 'world_math_profile.h', 'world_lut_math.h', 'world_math_tables.h', 'world_float_bits.h', 'world_activation.h', 'world_activation_tables.h', 'world_playback.h'):
        copy_changed(here / 'native' / name, component / name)
    for path in (here / 'native/esp32').iterdir():
        if path.is_file():
            copy_changed(path, component / path.name)
    weights = cache / 'irodori-world-envelope-baseline-20261113/fp32/weights.f32'
    previous = cache / 'irodori-world-native-20261113/analysis'
    report = json.loads((previous / 'report.json').read_text())
    key = os.environ.get('SUMI_WORLD_BENCH_FIXTURE', 'mf07-expand_test_03')
    if key not in ('mf07-expand_test_03', 'mf07-expand_test_06'):
        raise ValueError('Unsupported diagnostic fixture')
    if key != 'mf07-expand_test_03' and not (sample_rate == 16000 and full_wave):
        raise ValueError('Long fixture requires full-wave 16 kHz measurement')
    row = next(r for r in report['rows'] if r['id'] == key)
    fixture = previous / (key + '.input.f32')
    frames = fixture.stat().st_size // (32 * 4)
    if fixture.stat().st_size != frames * 32 * 4 or frames < 2:
        raise ValueError('Invalid diagnostic frame count')
    expected_weights = '75c2b800975f31b5204b926df5f1cef6eaabcd6ee5f684d8a66c9a19d194382b'
    for path, expected, name in ((weights, expected_weights, 'world_weights.bin'),
                                 (fixture, row['input_sha256'], 'world_fixture.bin')):
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError('WORLD diagnostic asset mismatch: ' + str(path))
        if name == 'world_weights.bin' and dsp:
            # 190 normalization floats precede every matrix. Eight prefix bytes
            # align all dense weight matrices to 16 without changing any values.
            write_changed(component / name, b'\0' * 8 + path.read_bytes())
        else:
            copy_changed(path, component / name)
    physical = previous / (key + '.chunks64.f32.physical.f32')
    if hashlib.sha256(physical.read_bytes()).hexdigest() != row['chunks64']['physical_sha256']:
        raise ValueError('WORLD reference physical features changed')
    copy_changed(physical, component / 'world_reference_features.bin')
    # A small raw prefix, prepared once from the audited FP64 wave. The host
    # environment for prepare.py does not require numpy.
    wave = cache / ('irodori-realtime-20261114/reference-full.f32' if full_wave else 'irodori-realtime-20261114/reference-prefix.f32')
    wave_hash = '01465d1735826de42d8e14faa6c7e1cef0f520f8e561c4f64030aec71e7b99d5' if full_wave else '90207554f4d48d6890a44bb5f34682c8bd3781d9145921f846c67cd325084956'
    if sample_rate == 16000:
        wave = cache / 'irodori-realtime-20261114/reference-full-16000.f32'
        wave_hash = 'e894b65296c14afc4c8ccac19813576164958ce7d7135d6dab1d51c408d7bcde'
    if key == 'mf07-expand_test_06':
        wave = cache / 'irodori-realtime-20261114/reference-test06-16000.f32'
        wave_hash = '6737d244739ba50ebb041ac82927310f83c7fd0378ddd24f19ac09b042b98246'
    if hashlib.sha256(wave.read_bytes()).hexdigest() != wave_hash:
        raise ValueError('WORLD reference waveform changed')
    copy_changed(wave, component / 'world_reference_wave.bin')
    write_changed(cmake, b'''idf_component_register(
    SRCS "world_envelope.cpp" "world_stream.cpp" "sumi_world_bench.cpp"
         "world/common.cpp" "world/fft.cpp" "world/matlabfunctions.cpp"
    INCLUDE_DIRS "." "world"
    PRIV_REQUIRES esp_timer esp_pm)
target_add_binary_data(${COMPONENT_LIB} "world_weights.bin" BINARY ALIGN 16)
target_add_binary_data(${COMPONENT_LIB} "world_fixture.bin" BINARY ALIGN 16)
target_add_binary_data(${COMPONENT_LIB} "world_reference_features.bin" BINARY ALIGN 16)
target_add_binary_data(${COMPONENT_LIB} "world_reference_wave.bin" BINARY ALIGN 16)
target_compile_options(${COMPONENT_LIB} PRIVATE -O2 -ffp-contract=off -fno-fast-math
    -Wno-missing-field-initializers)
''')
    if fp32:
        with cmake.open('a') as out:
            out.write('target_compile_definitions(${COMPONENT_LIB} PUBLIC SUMI_WORLD_FP32=1)\n')
    if dsp:
        dsp_source = prepare_dsp()
        shutil.copytree(dsp_source, esp / 'components/esp-dsp', dirs_exist_ok=True)
        text = cmake.read_text().replace('PRIV_REQUIRES esp_timer esp_pm)', 'PRIV_REQUIRES esp_timer esp_pm esp-dsp)')
        text += 'target_compile_definitions(${COMPONENT_LIB} PRIVATE SUMI_WORLD_ESP_DSP=1)\n'
        write_changed(cmake, text.encode())
    if dsp_fft:
        text = cmake.read_text().replace('"world/fft.cpp"', '"world_fft_dsp.cpp"')
        text += 'target_compile_definitions(${COMPONENT_LIB} PUBLIC SUMI_WORLD_FFT_DSP=1)\n'
        write_changed(cmake, text.encode())
    if hot_code:
        text = cmake.read_text().replace('    INCLUDE_DIRS', '    LDFRAGMENTS "world_hot_code.lf"\n    INCLUDE_DIRS', 1)
        write_changed(cmake, text.encode())
    for enabled_flag, define in ((compact, 'SUMI_WORLD_COMPACT_WORKSPACE'),
                                 (internal_stack, 'SUMI_WORLD_BENCH_INTERNAL_STACK'),
                                 (phase_recurrence, 'SUMI_WORLD_PHASE_RECURRENCE'),
                                 (silu_table, 'SUMI_WORLD_SILU_TABLE'),
                                 (pipeline, 'SUMI_WORLD_BENCH_PIPELINE'),
                                 (playback, 'SUMI_WORLD_BENCH_PLAYBACK')):
        if enabled_flag:
            text = cmake.read_text() + f'target_compile_definitions(${{COMPONENT_LIB}} PUBLIC {define}=1)\n'
            write_changed(cmake, text.encode())
    text = cmake.read_text() + f'target_compile_definitions(${{COMPONENT_LIB}} PUBLIC SUMI_WORLD_BENCH_STACK_BYTES={stack_bytes} SUMI_WORLD_MATRIX_TILE_FRAMES={tile_frames})\n'
    if pipeline:
        text += f'target_compile_definitions(${{COMPONENT_LIB}} PRIVATE SUMI_WORLD_BENCH_WAVE_PRIORITY={wave_priority})\n'
        text += 'target_compile_definitions(${COMPONENT_LIB} PRIVATE SUMI_WORLD_INTERNAL_RESERVE=10000)\n'
    if filter_stride:
        text += f'target_compile_definitions(${{COMPONENT_LIB}} PUBLIC SUMI_WORLD_FILTER_STRIDE={filter_stride})\n'
    write_changed(cmake, text.encode())
    if bounded_math:
        text = cmake.read_text() + 'target_compile_definitions(${COMPONENT_LIB} PUBLIC SUMI_WORLD_BOUNDED_MATH=1)\n'
        write_changed(cmake, text.encode())
    if math_profile:
        text = cmake.read_text() + 'target_compile_definitions(${COMPONENT_LIB} PUBLIC SUMI_WORLD_MATH_PROFILE=1)\n'
        write_changed(cmake, text.encode())
    if lut_math:
        text = cmake.read_text() + 'target_compile_definitions(${COMPONENT_LIB} PUBLIC SUMI_WORLD_LUT_MATH=1)\n'
        write_changed(cmake, text.encode())
    text = cmake.read_text() + f'target_compile_definitions(${{COMPONENT_LIB}} PRIVATE SUMI_WORLD_BENCH_CHUNK={chunk})\n'
    samples = sample_rate * (frames - 1) // 200 if full_wave else sample_rate
    if wave.stat().st_size != samples * 4:
        raise ValueError('Reference waveform length mismatch')
    text += f'target_compile_definitions(${{COMPONENT_LIB}} PRIVATE SUMI_WORLD_BENCH_FRAMES={frames})\n'
    text += f'target_compile_definitions(${{COMPONENT_LIB}} PRIVATE SUMI_WORLD_BENCH_SAMPLES={samples})\n'
    text += f'target_compile_definitions(${{COMPONENT_LIB}} PUBLIC SUMI_WORLD_SAMPLE_RATE={sample_rate})\n'
    text += f'target_compile_definitions(${{COMPONENT_LIB}} PUBLIC SUMI_WORLD_SYNTH_FFT_SIZE={fft_size})\n'
    write_changed(cmake, text.encode())
    muse_cmake = esp / 'components/muse/CMakeLists.txt'
    original = muse_cmake.read_text()
    marker = 'set(muse_priv_requires\n'
    if original.count(marker) != 1:
        raise ValueError('Muse component layout changed')
    original = original.replace(marker, marker + '    sumi_world_bench\n')
    original += '\n# Explicit local diagnostic build; normal firmware omits this code.\n'
    original += 'target_compile_definitions(${COMPONENT_LIB} PRIVATE SUMI_WORLD_BENCH=1)\n'
    write_changed(muse_cmake, original.encode())
    if playback:
        voice = esp / 'components/muse/muse_voice.c'
        text = voice.read_text()
        marker = 'static void voice_task(void *arg)'
        if text.count(marker) != 1:
            raise ValueError('Voice task layout changed')
        text = text.replace(marker, '''static bool sumi_benchmark_write(const int16_t *data, size_t count)
{
    return muse_audio_write(data, count) == ESP_OK;
}

''' + marker)
        marker = '    for (;;) {\n        muse_input_event_t head;'
        if text.count(marker) != 1:
            raise ValueError('Voice loop layout changed')
        text = text.replace(marker, '''    for (;;) {
        if (sumi_world_bench_audio_active()) {
            set_resting(false);
            bool stop = atomic_load(&s_shutdown);
            muse_input_event_t ignored;
            while (xQueueReceive(s_queue, &ignored, 0) == pdTRUE) {
                muse_input_clear_call_pending(ignored.mark);
                stop = true;
            }
            sumi_world_bench_audio_step(stop, muse_audio_is_on(),
                sumi_benchmark_write, muse_audio_quiet);
            pending_down = false;
            s_pre_fill = 0;
            s_settle = SETTLE_CHUNKS;
            vTaskDelay(1);
            continue;
        }
        muse_input_event_t head;''')
        text = text.replace('while (muse_voice_in_call() && esp_timer_get_time() < until)',
                            'while ((muse_voice_in_call() || sumi_world_bench_audio_active()) && esp_timer_get_time() < until)')
        write_changed(voice, ('#include "sumi_world_bench.h"\n' + text).encode())

        console = esp / 'components/muse/muse_input.c'
        text = console.read_text()
        marker = 'if (!strcmp(line, "sumi.tts_bench")) {'
        if text.count(marker) != 1:
            raise ValueError('Benchmark console layout changed')
        text = text.replace(marker, '''if (!strncmp(line, "sumi.tts_bench", 14) && (line[14] == 0 || line[14] == ' ')) {''')
        marker = 'whole && idle && sumi_tts_benchmark()'
        if text.count(marker) != 1:
            raise ValueError('Benchmark request layout changed')
        text = text.replace(marker, 'whole && idle && sumi_benchmark_delay(line + 14) && sumi_tts_benchmark()')
        marker = 'static bool console_command(char *line, bool whole)'
        helper = '''static bool sumi_benchmark_delay(const char *suffix)
{
    char *end = NULL;
    long delay = *suffix ? strtol(suffix, &end, 10) : 3000;
    return (!*suffix || (end != suffix && !*end)) && delay >= 1000 && delay <= 5000 &&
        sumi_world_bench_audio_delay((unsigned)delay);
}

'''
        if text.count(marker) != 1:
            raise ValueError('Console function layout changed')
        text = text.replace(marker, helper + marker)
        write_changed(console, ('#include "sumi_world_bench.h"\n' + text).encode())
