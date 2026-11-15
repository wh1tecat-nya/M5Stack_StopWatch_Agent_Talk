package jp.sumi.stopwatch;

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
    record ChatRequest(String text, int chat, boolean wearable) {}

    interface Asr {
        Task transcribe(Recording recording, Result<String> result);
    }
    interface Tts {
        default void warmUp() {}
        Task synthesize(Utterance utterance, Result<Audio> result);
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
