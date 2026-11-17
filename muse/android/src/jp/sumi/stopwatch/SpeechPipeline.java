package jp.sumi.stopwatch;

import android.os.Handler;
import android.util.Log;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;
import java.util.concurrent.CompletableFuture;
import java.util.function.Consumer;
import java.util.function.Supplier;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Future;

final class SpeechPipeline implements StopwatchAudio.Speech {
    private static final long GENERATION_LIMIT_MS = 35000;
    private static final int SHORT_REPLY = 22;
    private static final int MAX_SEGMENT = 36;
    private static final int FIRST_PAUSE_MIN = 6;
    private final Handler handler;
    private final Supplier<SpeechBackend.Providers> config;
    private final Consumer<String> status;
    private final ExecutorService workers;
    private final ConversationLog log = ConversationLog.INSTANCE;
    private SpeechBackend.Task transcriptionRequest;
    private SpeechBackend.Task chatRequest;
    private CompletableFuture<String> pendingTranscript;
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
        var transcript = new CompletableFuture<String>();
        pendingTranscript = transcript;
        var playback = new Playback(id, providers.tts(), output);
        status.accept("文字起こし中…");
        transcriptionRequest = providers.asr().transcribe(new SpeechBackend.Recording(note.pcm(), 16000, "ja-JP"),
                result(id, heard -> {
                    transcriptionRequest = null;
                    if (heard.isBlank()) { fail.accept("発話を認識できませんでした"); return; }
                    long asrMs = elapsedMs(started);
                    log.body(asr, "文字起こし · " + asrMs + " ms", heard);
                    Log.i("SumiLatency", "BLE ASR in " + asrMs + " ms");
                    playback.transcribedAt = System.nanoTime();
                    status.accept("Museの返答を待っています…");
                    pendingTranscript = null;
                    transcript.complete(heard);
                }, reason -> {
                    transcriptionRequest = null;
                    log.event(asr, reason);
                    fail.accept(reason);
                }));
        Consumer<String> completed = output.streamAvailable() ? text -> handler.post(() -> {
            if (id != generation) return;
            if (playback.text.isEmpty())
                Log.i("SumiLatency", "First complete Muse message in " + elapsedMs(playback.transcribedAt) + " ms");
            playback.accept(text, false);
        }) : null;
        chatRequest = providers.chat().reply(new SpeechBackend.ChatRequest(transcript, note.session(), note.wearable(), "", completed),
                result(id, text -> {
                    chatRequest = null;
                    if (text.isBlank()) { fail.accept("Museから返答本文が届きませんでした"); return; }
                    long waiting = elapsedMs(playback.transcribedAt);
                    Log.i("SumiTts", "Muse reply in " + waiting + " ms");
                    log.event(ConversationLog.MUSE, "返答完了 · " + waiting + " ms");
                    playback.accept(text, true);
                }, reason -> {
                    chatRequest = null;
                    log.event(ConversationLog.MUSE, reason);
                    fail.accept(reason);
                }));
    }

    void playExternal(String text, StopwatchAudio.Reply output) {
        cancel();
        new Playback(generation, config.get().tts(), output).accept(text, true);
    }

    private final class Playback {
        private final long id;
        private final SpeechBackend.Tts tts;
        private final StopwatchAudio.Reply output;
        private final List<String> segments = new ArrayList<>();
        private long transcribedAt;
        private String text = "";
        private int rendered;
        private int prefixBytes;
        private boolean sending, synthesizing, allText, streamOpen, finalSent;
        private byte[] ready;

        Playback(long id, SpeechBackend.Tts tts, StopwatchAudio.Reply output) {
            this.id = id;
            this.tts = tts;
            this.output = output;
        }

        void accept(String next, boolean last) {
            if (!next.startsWith(text)) {
                fail("Museの返答が途中で変わりました");
                return;
            }
            String added = next.substring(text.length()).strip();
            if (!added.isEmpty()) {
                text = next;
                output.caption(text);
                segments.addAll(output.streamAvailable() ? segments(added, segments.isEmpty()) : List.of(added));
            }
            allText = last;
            advance();
        }

        private void advance() {
            if (id != generation || finalSent) return;
            sendReady();
            if (id != generation || finalSent) return;
            if (!synthesizing && ready == null && rendered < segments.size()) start();
            else if (allText && streamOpen && !sending && !synthesizing && ready == null && rendered == segments.size()) {
                finalSent = true;
                output.finishStream();
            }
        }

        void start() {
            if (id != generation) return;
            synthesizing = true;
            output.generating();
            if (!sending) showGenerating();
            long started = System.nanoTime();
            log.body(ConversationLog.IRODORI, "生成入力 · " + (rendered + 1) + "/" + segments.size(), segments.get(rendered));
            generationTimeout = () -> {
                if (id == generation) fail("音声生成が35秒を超えました。短い返答で試してください");
            };
            handler.postDelayed(generationTimeout, GENERATION_LIMIT_MS);
            String phrase = segments.get(rendered);
            if (rendered == 0) Log.i("SumiLatency", "First TTS phrase: " + phrase.codePointCount(0, phrase.length()) + " characters");
            var utterance = new SpeechBackend.Utterance(phrase, "ja-JP");
            var complete = result(id, (SpeechBackend.Audio speech) -> {
                int convertedPrefix = prefixBytes;
                Future<?> conversion = workers.submit(() -> {
                    try {
                        byte[] pcm = Pcm.tailTo16k(speech, convertedPrefix);
                        handler.post(() -> generated(pcm, started));
                    } catch (RuntimeException e) {
                        handler.post(() -> {
                            if (id == generation) fail("返答の音声形式が不正です");
                        });
                    }
                });
                task = () -> conversion.cancel(true);
            }, this::fail);
            if (rendered == 0 && output.streamAvailable()) {
                task = tts.synthesize(utterance, output.prefillAvailable(), new Consumer<SpeechBackend.Prefix>() {
                    private int convertedBytes;

                    @Override public void accept(SpeechBackend.Prefix prefix) {
                        try {
                            byte[] pcm = Pcm.prefixTo16k(prefix.audio(), convertedBytes);
                            convertedBytes += pcm.length;
                            handler.post(() -> sendPrefix(pcm, prefix.playable(), started));
                        } catch (IllegalArgumentException ignored) {
                            // The final waveform is validated independently.
                        }
                    }
                }, complete);
            } else task = tts.synthesize(utterance, complete);
        }

        private void sendPrefix(byte[] pcm, boolean playable, long started) {
            if (id != generation || rendered != 0) return;
            if (pcm.length != 0) {
                boolean first = prefixBytes == 0;
                queueAudio(pcm);
                prefixBytes += pcm.length;
                long elapsed = elapsedMs(started);
                String event = first ? "先頭音声の転送準備" : "先行音声を追加";
                Log.i("SumiTts", event + " in " + elapsed + " ms, bytes=" + prefixBytes);
                log.event(ConversationLog.IRODORI, event + " · " + elapsed + " ms");
            }
            if (playable) output.allowPlayback();
            advance();
        }

        private void queueAudio(byte[] pcm) {
            if (ready == null) ready = pcm;
            else {
                int prior = ready.length;
                ready = Arrays.copyOf(ready, prior + pcm.length);
                System.arraycopy(pcm, 0, ready, prior, pcm.length);
            }
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
            synthesizing = false;
            ++rendered;
            int bytes = prefixBytes + pcm.length;
            Log.i("SumiTts", "segment " + rendered + "/" + segments.size() + " generated in " +
                    elapsedMs(started) + " ms, PCM bytes=" + bytes);
            log.event(ConversationLog.IRODORI, "生成完了 · " + rendered + "/" + segments.size() + " · " +
                    elapsedMs(started) + " ms · " + bytes + " B");
            if (bytes == 0) {
                fail("返答の音声形式が不正です");
                return;
            }
            if (pcm.length != 0) queueAudio(pcm);
            if (rendered == 1) output.allowPlayback();
            prefixBytes = 0;
            advance();
        }

        private void sendReady() {
            if (sending || ready == null) return;
            byte[] pcm = ready;
            ready = null;
            sending = true;
            boolean last = allText && rendered == segments.size();
            if (last && !streamOpen && segments.size() == 1) {
                finalSent = true;
                output.play(pcm);
                return;
            }
            streamOpen = true;
            finalSent = last;
            output.segment(pcm, last, this::sent);
        }

        private void sent() {
            if (id != generation) return;
            sending = false;
            if (synthesizing && ready == null) showGenerating();
            advance();
        }
    }

    private static long elapsedMs(long start) {
        return (System.nanoTime() - start) / 1_000_000;
    }

    static List<String> segments(String text, boolean firstAudio) {
        var parts = new ArrayList<String>();
        text = text.replaceAll("[\\p{So}\\uFE0F\\u200D]+", "").strip();
        if (firstAudio) {
            int cut = firstPause(text);
            if (cut != 0) {
                parts.add(text.substring(0, cut).stripTrailing());
                text = text.substring(cut).stripLeading();
            }
        }
        if (text.codePointCount(0, text.length()) <= SHORT_REPLY) {
            parts.add(text);
            return parts;
        }
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

    private static int firstPause(String text) {
        int count = text.codePointCount(0, text.length());
        int end = 0;
        int limit = Math.min(SHORT_REPLY, count - FIRST_PAUSE_MIN);
        for (int length = 1; length <= limit; length++) {
            int point = text.codePointAt(end);
            end += Character.charCount(point);
            if (length >= FIRST_PAUSE_MIN && "、。！？!?".indexOf(point) >= 0 &&
                    "、。！？!?」』）】〉》".indexOf(text.codePointAt(end)) < 0) return end;
        }
        return 0;
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
        if (transcriptionRequest != null) transcriptionRequest.cancel();
        transcriptionRequest = null;
        if (chatRequest != null) chatRequest.cancel();
        chatRequest = null;
        if (pendingTranscript != null) pendingTranscript.cancel(false);
        pendingTranscript = null;
        if (task != null) task.cancel();
        task = null;
    }
}
