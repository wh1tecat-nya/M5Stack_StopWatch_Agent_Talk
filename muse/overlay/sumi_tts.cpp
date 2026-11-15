#include "sumi_tts.h"
#include "sumi_core.h"
#include "sumi_speech_text.h"
#include "SanoTTS.h"
#include "core/openjtalk/text2mecab.h"
#ifdef SUMI_WORLD_BENCH
#include "sumi_world_bench.h"
#endif
#include <atomic>
#include <cstdlib>
#include <cstring>
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/idf_additions.h"
#include "freertos/queue.h"
#include "freertos/task.h"
#ifdef ESP_PLATFORM
#include "esp_private/esp_clk.h"
#endif

namespace {
struct Job { uint32_t generation; char *text; int speed; bool benchmark; };
struct Packet { uint32_t generation; uint16_t count; bool done, failed; int16_t pcm[256]; };
struct Output {
    Packet packet{}; sumi_resampler_t rate{}; size_t emitted{}, source_samples{}, fullscale{};
    int peak{};
    int64_t queue_wait_us{};
    int64_t callback_us{}, chunk_started{}, first_pcm_us{-1};
    bool benchmark{};
};
std::atomic<uint32_t> generation{1};
std::atomic<uint32_t> primed{0};
constexpr size_t audio_packets = 128;
constexpr size_t preroll_samples = 16000 * 1600 / 1000;
static_assert(preroll_samples < audio_packets * 256, "pre-roll must fit the queue");
std::atomic<int> speed{80};
QueueHandle_t jobs, audio;
SanoTTS voice;

char *speech_copy(const char *text) {
    size_t n = sumi_speech_readings(text, nullptr, 0);
    if (n == SIZE_MAX || n > (SIZE_MAX - 1) / 3) return nullptr;
    char *words = static_cast<char *>(heap_caps_malloc(n + 1, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT));
    if (!words) return nullptr;
    if (sumi_speech_readings(text, words, n + 1) != n || !sumi_speech_text(words)) {
        free(words); return nullptr;
    }
    /* Dictionary entries use full-width letters/digits. Skipping this
       Open JTalk preprocessing silently drops ASCII unknown words/numbers. */
    size_t capacity = strlen(words) * 3 + 1;
    char *copy = static_cast<char *>(heap_caps_malloc(capacity, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT));
    bool ok = copy && text2mecab(copy, capacity, words) == TEXT2MECAB_RESULT_SUCCESS;
    free(words);
    if (!ok) { free(copy); return nullptr; }
    return copy;
}

struct Initialization { TaskHandle_t waiter; bool ok; };
void initialize_voice(void *context) {
    auto &job = *static_cast<Initialization *>(context);
    job.ok = voice.begin() && voice.kanjiReady();
    xTaskNotifyGive(job.waiter);
    vTaskDeleteWithCaps(nullptr);
}
bool ready_voice() {
    if (voice.ready()) return voice.kanjiReady();
    /* Mapping the flash dictionary briefly disables the cache. IDF requires
       an internal stack for that operation; synthesis itself can use PSRAM. */
    Initialization job{xTaskGetCurrentTaskHandle(), false};
    if (xTaskCreateWithCaps(initialize_voice, "sumi_tts_init", 8192, &job, 3, nullptr,
                           MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT) != pdPASS) {
        ESP_LOGE("sumi_tts", "cannot start dictionary initialization task");
        return false;
    }
    ulTaskNotifyTake(pdTRUE, portMAX_DELAY);
    return job.ok;
}

bool send(Output &out) {
    if (out.benchmark) { out.packet.count = 0; return generation.load() == out.packet.generation; }
    while (generation.load() == out.packet.generation) {
        int64_t started = esp_timer_get_time();
        bool sent = xQueueSend(audio, &out.packet, pdMS_TO_TICKS(20)) == pdTRUE;
        out.queue_wait_us += esp_timer_get_time() - started;
        if (sent) {
            if (out.emitted >= preroll_samples || out.packet.done) primed.store(out.packet.generation);
            out.packet.count = 0;
            return true;
        }
    }
    return false;
}
void append(const int16_t *pcm, size_t n, void *context) {
    auto &out = *static_cast<Output *>(context);
    if (out.packet.generation != generation.load()) return;
    int64_t started = esp_timer_get_time();
    if (out.first_pcm_us < 0) out.first_pcm_us = started - out.chunk_started;
    for (size_t i = 0; i < n; ++i) {
        int amplitude = std::abs(static_cast<int>(pcm[i]));
        if (amplitude > out.peak) out.peak = amplitude;
        out.fullscale += amplitude >= 32767;
        ++out.source_samples;
        int16_t sample;
        if (!sumi_resample_one(&out.rate, pcm[i], &sample)) continue;
        out.packet.pcm[out.packet.count++] = sample;
        ++out.emitted;
        if (out.packet.count == 256 && !send(out)) break;
    }
    out.callback_us += esp_timer_get_time() - started;
}
bool speak(const char *text, size_t n, Output &out) {
    if (out.packet.generation != generation.load()) return false;
    while (n && *text == ' ') { ++text; --n; }
    while (n && text[n - 1] == ' ') --n;
    if (!n) return true;
    size_t before = out.emitted;
    int64_t callback_before = out.callback_us;
    out.chunk_started = esp_timer_get_time(); out.first_pcm_us = -1;
    bool ok = voice.synthesize(text, n, append, &out);
    ESP_LOGI("sumi_tts", "chunk bytes=%u output=%.2fs total=%.3fs first=%.3fs callback=%.3fs ok=%d",
        (unsigned)n, (out.emitted - before) / 16000.0,
        (esp_timer_get_time() - out.chunk_started) / 1e6, out.first_pcm_us / 1e6,
        (out.callback_us - callback_before) / 1e6, ok);
    if (ok) return true;
    /* Length/ID limits can be hit by numbers or long readings. Retry only
       before any audio escaped, and always on a UTF-8 boundary. */
    size_t cut = n / 2;
    while (cut && ((unsigned char)text[cut] & 0xc0) == 0x80) --cut;
    if (out.emitted != before || n <= 6 || !cut) return false;
    return speak(text, cut, out) && speak(text + cut, n - cut, out);
}
void worker(void *) {
    Job job;
    for (;;) {
        if (xQueueReceive(jobs, &job, portMAX_DELAY) != pdTRUE) continue;
#ifdef SUMI_WORLD_BENCH
        if (job.benchmark) {
            sumi_world_bench_run([](uint32_t expected) { return generation.load() == expected; }, job.generation);
            sumi_world_bench_audio_release();
            free(job.text);
            continue;
        }
#endif
        Output out;
        out.packet.generation = job.generation;
        out.benchmark = job.benchmark;
#ifdef ESP_PLATFORM
        ESP_LOGI("sumi_tts", "start: CPU=%u MHz core=%d benchmark=%d PIE=%d",
            (unsigned)(esp_clk_cpu_freq() / 1000000), xPortGetCoreID(), job.benchmark, SANOTTS_ENABLE_PIE);
#endif
        bool ok = job.generation == generation.load();
        if (ok) ok = ready_voice();
        int64_t render_started = esp_timer_get_time();
        voice.setLengthScale(1.2187f * 1.15f * 100.0f / job.speed);
        for (size_t pos = 0; ok && job.text[pos];) {
            size_t n = sumi_utf8_chunk(job.text + pos, 180);
            ok = n && speak(job.text + pos, n, out);
            pos += n;
        }
        free(job.text);
        if (job.generation != generation.load()) continue;
        if (out.packet.count) send(out);
        int64_t render_us = esp_timer_get_time() - render_started - out.queue_wait_us;
        out.packet.done = true;
        out.packet.failed = !ok;
        ESP_LOGI("sumi_tts", "speech: rate=%d%% source=%u samples peak=%d fullscale=%u output=%.2fs",
                 job.speed, (unsigned)out.source_samples, out.peak, (unsigned)out.fullscale,
                 (double)out.emitted / 16000);
        ESP_LOGI("sumi_tts", "render %.3fs, queue wait %.3fs", render_us / 1e6, out.queue_wait_us / 1e6);
        if (!ok) ESP_LOGE("sumi_tts",
            "speech failed: %s (model=%d dict=%d psram=%u largest=%u internal=%u largest=%u)",
            voice.lastError(), voice.ready(), voice.kanjiReady(),
            (unsigned)heap_caps_get_free_size(MALLOC_CAP_SPIRAM),
            (unsigned)heap_caps_get_largest_free_block(MALLOC_CAP_SPIRAM),
            (unsigned)heap_caps_get_free_size(MALLOC_CAP_INTERNAL),
            (unsigned)heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL));
        send(out);
    }
}
bool init() {
    if (jobs) return true;
    auto jq = xQueueCreateWithCaps(1, sizeof(Job), MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    auto aq = xQueueCreateWithCaps(audio_packets, sizeof(Packet), MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (!jq || !aq) {
        if (jq) vQueueDeleteWithCaps(jq);
        if (aq) vQueueDeleteWithCaps(aq);
        return false;
    }
    jobs = jq; audio = aq;
    /* LVGL and I2S run on core 1. An unpinned floating-point task sticks
       to the core where it first uses the FPU, potentially behind LVGL.
       Keep synthesis on core 0; higher-priority network tasks can preempt it.
       DSP needs > 4 KB stack. PSRAM avoids starving radio DMA buffers. */
    if (xTaskCreatePinnedToCoreWithCaps(worker, "sumi_tts", 12288, nullptr, 3, nullptr, 0,
                            MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT) != pdPASS) {
        vQueueDeleteWithCaps(jobs); vQueueDeleteWithCaps(audio);
        jobs = audio = nullptr;
        return false;
    }
    return true;
}
}

extern "C" bool sumi_tts_start(const char *text) {
    if (!text || !*text || !init()) return false;
    char *copy = speech_copy(text);
    if (!copy) return false;
    sumi_tts_cancel();
    Job job{generation.load(), copy, speed.load(), false}, pending;
    if (xQueueReceive(jobs, &pending, 0) == pdTRUE) free(pending.text);
    if (xQueueSend(jobs, &job, 0) != pdTRUE) { free(copy); return false; }
    return true;
}
extern "C" bool sumi_tts_benchmark(void) {
    /* Fixed local input. A separately opted-in WORLD build can play locally. */
    static const char text[] = "うん、聞こえるよ、しろねこ。今度は言葉もすごく綺麗に届いたよ。"
        "M5Stackからの会話テストだよ、今度こそ声が聞こえるかな、って、ばっちりだよ。"
        "夜遅くまで改良してくれてた成果、出てる感じがするなぁ。";
    if (!init()) return false;
    char *copy = speech_copy(text);
    if (!copy) return false;
#ifdef SUMI_WORLD_BENCH
    if (!sumi_world_bench_audio_reserve()) { free(copy); return false; }
#endif
    sumi_tts_cancel();
    Job job{generation.load(), copy, speed.load(), true}, pending;
    if (xQueueReceive(jobs, &pending, 0) == pdTRUE) free(pending.text);
    if (xQueueSend(jobs, &job, 0) != pdTRUE) {
#ifdef SUMI_WORLD_BENCH
        sumi_world_bench_audio_release();
#endif
        free(copy); return false;
    }
    return true;
}
extern "C" void sumi_tts_cancel(void) { generation.fetch_add(1); }
extern "C" int sumi_tts_rate(void) { return speed.load(); }
extern "C" void sumi_tts_set_rate(int percent) {
    if (percent >= 60 && percent <= 120) speed.store(percent);
}
extern "C" size_t sumi_tts_read(int16_t pcm[256], bool *done, bool *failed) {
    *done = *failed = false;
    if (!audio) return 0;
    Packet p;
    while (xQueuePeek(audio, &p, 0) == pdTRUE) {
        if (p.generation != generation.load()) {
            xQueueReceive(audio, &p, 0); /* Cancellation must drain stale audio even before the new pre-roll. */
            continue;
        }
        if (primed.load() != p.generation) return 0;
        if (xQueueReceive(audio, &p, 0) != pdTRUE) return 0;
        if (p.generation != generation.load()) continue;
        memcpy(pcm, p.pcm, p.count * sizeof(int16_t));
        *done = p.done; *failed = p.failed;
        return p.count;
    }
    return 0;
}
