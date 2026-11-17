package jp.sumi.stopwatch;

import java.util.concurrent.CompletableFuture;
import java.util.function.Consumer;

final class SpeechBackend {
    interface Task {
        void cancel();
    }
    interface Result<T> {
        void success(T value);
        void failure(String reason);
    }

    record Recording(byte[] pcm16, int sampleRate, String language) {}
    record Utterance(String text, String language) {}
    record Audio(byte[] pcm16, int sampleRate) {}
    record Prefix(Audio audio, boolean playable) {}
    /** Providers may prepare a connection while transcription is pending. Only completed text may be sent. */
    record ChatRequest(CompletableFuture<String> transcript, String session, boolean wearable, String catalogId,
                       Consumer<String> completedText) {
        ChatRequest(String text, String session, boolean wearable, String catalogId) {
            this(CompletableFuture.completedFuture(text), session, wearable, catalogId, null);
        }
        boolean catalog() { return !catalogId.isEmpty(); }
    }

    interface Asr {
        Task transcribe(Recording recording, Result<String> result);
    }
    interface Tts {
        default void warmUp() {}
        Task synthesize(Utterance utterance, Result<Audio> result);

        /** Reports growing, immutable prefixes in order before completion. Prefill allows
         * delivery to the device while playback is still held. */
        default Task synthesize(Utterance utterance, boolean prefill, Consumer<Prefix> prefix, Result<Audio> result) {
            return synthesize(utterance, result);
        }
    }
    interface Chat {
        Task reply(ChatRequest request, Result<String> result);
    }

    record Providers(Asr asr, Chat chat, Tts tts) {}
    record Provider<T>(String label, T backend) {}

    static <T> Result<T> result(Consumer<T> success, Consumer<String> failure) {
        return new Result<>() {
            @Override public void success(T value) { success.accept(value); }
            @Override public void failure(String reason) { failure.accept(reason); }
        };
    }

    private SpeechBackend() {}
}
