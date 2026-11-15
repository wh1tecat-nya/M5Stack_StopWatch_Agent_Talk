package jp.sumi.stopwatch;

import android.os.Handler;
import android.util.Log;
import java.util.ArrayList;
import java.util.List;
import java.util.function.Consumer;
import java.util.function.Supplier;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Future;

final class SpeechPipeline implements StopwatchAudio.Speech {
    private static final long GENERATION_LIMIT_MS = 35000;
    private static final int SHORT_REPLY = 22;
    private static final int MAX_SEGMENT = 36;
    private final Handler handler;
    private final Supplier<SpeechBackend.Providers> config;
    private final Consumer<String> status;
    private final ExecutorService workers;
    private final ConversationLog log = ConversationLog.INSTANCE;
    private SpeechBackend.Task task;
    private Runnable generationTimeout;
    private long generation;

    SpeechPipeline(Handler handler, Supplier<SpeechBackend.Providers> config, Consumer<String> status, ExecutorService workers) {
        this.handler = handler;
        this.config = config;
        this.status = status;
        this.workers = workers;
    }

    @Override public void start(StopwatchAudio.Recording note, StopwatchAudio.Reply output) {
        cancel();
        long id = generation;
        long started = System.nanoTime();
        SpeechBackend.Providers providers = config.get();
        String asr = providers.asr() instanceof OpenAiAsr ? ConversationLog.OPENAI : ConversationLog.ANDROID;
        providers.tts().warmUp();
        Consumer<String> fail = reason -> {
            if (id == generation) output.fail(reason);
        };
        status.accept("文字起こし中…");
        task = providers.asr().transcribe(new SpeechBackend.Recording(note.pcm(), 16000, "ja-JP"),
                result(id, heard -> {
                    if (heard.isBlank()) { fail.accept("発話を認識できませんでした"); return; }
                    log.body(asr, "文字起こし · " + elapsedMs(started) + " ms", heard);
                    long waiting = System.nanoTime();
                    status.accept("Museの返答を待っています…");
                    task = providers.chat().reply(new SpeechBackend.ChatRequest(heard, note.chat(), note.wearable()),
                            result(id, text -> {
                                if (text.isBlank()) { fail.accept("Museから返答本文が届きませんでした"); return; }
                                Log.i("SumiTts", "Muse reply in " + elapsedMs(waiting) + " ms");
                                log.event(ConversationLog.MUSE, "返答完了 · " + elapsedMs(waiting) + " ms");
                                output.caption(text);
                                List<String> segments = output.streamAvailable() ? segments(text) : List.of(text);
                                new Playback(id, providers.tts(), output, segments).start();
                            }, reason -> {
                                log.event(ConversationLog.MUSE, reason);
                                fail.accept(reason);
                            }));
                }, reason -> {
                    log.event(asr, reason);
                    fail.accept(reason);
                }));
    }

    private final class Playback {
        private final long id;
        private final SpeechBackend.Tts tts;
        private final StopwatchAudio.Reply output;
        private final List<String> segments;
        private int rendered;
        private boolean sending;
        private byte[] ready;

        Playback(long id, SpeechBackend.Tts tts, StopwatchAudio.Reply output, List<String> segments) {
            this.id = id;
            this.tts = tts;
            this.output = output;
            this.segments = segments;
        }

        void start() {
            if (id != generation) return;
            output.generating();
            if (!sending) showGenerating();
            long started = System.nanoTime();
            log.body(ConversationLog.IRODORI, "生成入力 · " + (rendered + 1) + "/" + segments.size(), segments.get(rendered));
            generationTimeout = () -> {
                if (id == generation) fail("音声生成が35秒を超えました。短い返答で試してください");
            };
            handler.postDelayed(generationTimeout, GENERATION_LIMIT_MS);
            task = tts.synthesize(new SpeechBackend.Utterance(segments.get(rendered), "ja-JP"), result(id, speech -> {
                Future<?> conversion = workers.submit(() -> {
                    try {
                        byte[] pcm = Pcm.to16k(speech);
                        handler.post(() -> generated(pcm, started));
                    } catch (RuntimeException e) {
                        handler.post(() -> {
                            if (id == generation) fail("返答の音声形式が不正です");
                        });
                    }
                });
                task = () -> conversion.cancel(true);
            }, this::fail));
        }

        private void fail(String reason) {
            log.event(ConversationLog.IRODORI, reason);
            output.fail(reason);
        }

        private void showGenerating() {
            status.accept("スマホで音声を生成中（" + (rendered + 1) + "/" + segments.size() + "）…");
        }

        private void generated(byte[] pcm, long started) {
            if (id != generation) return;
            clearGenerationTimer();
            task = null;
            ++rendered;
            Log.i("SumiTts", "segment " + rendered + "/" + segments.size() + " generated in " +
                    elapsedMs(started) + " ms, PCM bytes=" + pcm.length);
            log.event(ConversationLog.IRODORI, "生成完了 · " + rendered + "/" + segments.size() + " · " +
                    elapsedMs(started) + " ms · " + pcm.length + " B");
            if (segments.size() == 1) {
                output.play(pcm);
                return;
            }
            ready = pcm;
            sendReady();
        }

        private void sendReady() {
            if (sending || ready == null) return;
            byte[] pcm = ready;
            ready = null;
            sending = true;
            boolean last = rendered == segments.size();
            output.segment(pcm, last, this::sent);
            if (!last) start();
        }

        private void sent() {
            if (id != generation) return;
            sending = false;
            if (ready == null) showGenerating();
            else sendReady();
        }
    }

    private static long elapsedMs(long start) {
        return (System.nanoTime() - start) / 1_000_000;
    }

    static List<String> segments(String text) {
        var parts = new ArrayList<String>();
        text = text.replaceAll("[\\p{So}\\uFE0F\\u200D]+", "").strip();
        if (text.codePointCount(0, text.length()) <= SHORT_REPLY) return List.of(text);
        int start = 0, length = 0, breakAt = -1;
        for (int end = 0; end < text.length();) {
            int point = text.codePointAt(end);
            end += Character.charCount(point);
            length++;
            if ("、，,・ 　".indexOf(point) >= 0 && length >= 12) breakAt = end;
            boolean sentenceEnd = "。！？!?\n".indexOf(point) >= 0;
            if (length == MAX_SEGMENT || (sentenceEnd && (parts.isEmpty() || length >= 18))) {
                int cut = length == MAX_SEGMENT && !sentenceEnd && breakAt > start ? breakAt : end;
                String part = text.substring(start, cut).strip();
                if (!part.isEmpty()) parts.add(part);
                start = cut;
                end = cut;
                length = 0;
                breakAt = -1;
            }
        }
        String remaining = text.substring(start).strip();
        if (!remaining.isEmpty()) parts.add(remaining);
        return parts.isEmpty() ? List.of(text) : parts;
    }

    private void clearGenerationTimer() {
        if (generationTimeout != null) handler.removeCallbacks(generationTimeout);
        generationTimeout = null;
    }

    private <T> SpeechBackend.Result<T> result(long id, Consumer<T> success, Consumer<String> fail) {
        return SpeechBackend.result(value -> handler.post(() -> {
            if (id == generation) success.accept(value);
        }), reason -> handler.post(() -> {
            if (id == generation) fail.accept(reason);
        }));
    }

    @Override public void cancel() {
        ++generation;
        clearGenerationTimer();
        if (task != null) task.cancel();
        task = null;
    }
}
