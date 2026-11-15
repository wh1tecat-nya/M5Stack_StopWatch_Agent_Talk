package jp.sumi.stopwatch;

import org.json.JSONObject;

import java.io.BufferedOutputStream;
import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.net.HttpURLConnection;
import java.net.URI;
import java.nio.charset.StandardCharsets;
import java.util.UUID;
import java.util.concurrent.ExecutorService;
import java.util.function.Supplier;

final class OpenAiAsr implements SpeechBackend.Asr {
    private final Supplier<String> key;
    private final ExecutorService workers;

    OpenAiAsr(Supplier<String> key, ExecutorService workers) {
        this.key = key;
        this.workers = workers;
    }

    @Override public SpeechBackend.Task transcribe(SpeechBackend.Recording recording, SpeechBackend.Result<String> result) {
        String token = key.get();
        return NetworkTask.start(workers, result, "OpenAI音声認識に接続できません", request -> {
            if (token.isBlank()) throw new NetworkTask.Failure("アプリでOpenAI APIキーを設定してください");
            ConversationLog.INSTANCE.event(ConversationLog.OPENAI, "POST /v1/audio/transcriptions · gpt-transcribe · " +
                    recording.pcm16().length + " B");
            HttpURLConnection http = request.open(URI.create("https://api.openai.com/v1/audio/transcriptions"));
            String boundary = "sumi-" + UUID.randomUUID();
            String prefix = field(boundary, "model", "gpt-transcribe") + field(boundary, "languages[]", "ja") +
                    field(boundary, "keywords[]", "M5Stack") + field(boundary, "keywords[]", "Muse") +
                    field(boundary, "keywords[]", "StopWatch") + "--" + boundary +
                    "\r\nContent-Disposition: form-data; name=\"file\"; filename=\"voice.wav\"\r\n" +
                    "Content-Type: audio/wav\r\n\r\n";
            byte[] head = prefix.getBytes(StandardCharsets.UTF_8);
            byte[] end = ("\r\n--" + boundary + "--\r\n").getBytes(StandardCharsets.UTF_8);
            http.setRequestMethod("POST");
            http.setRequestProperty("Authorization", "Bearer " + token);
            http.setRequestProperty("Content-Type", "multipart/form-data; boundary=" + boundary);
            http.setDoOutput(true);
            http.setFixedLengthStreamingMode(head.length + 44 + recording.pcm16().length + end.length);
            try (var output = new BufferedOutputStream(http.getOutputStream())) {
                output.write(head);
                output.write(Pcm.waveHeader(recording.pcm16().length, recording.sampleRate()));
                output.write(recording.pcm16());
                output.write(end);
            }
            int code = http.getResponseCode();
            ConversationLog.INSTANCE.event(ConversationLog.OPENAI, "HTTP " + code);
            if (code != 200) throw new NetworkTask.Failure("OpenAI音声認識エラー（HTTP " + code + "）");
            try (var input = http.getInputStream()) {
                return new JSONObject(new String(readBounded(input, 65536), StandardCharsets.UTF_8))
                        .getString("text").strip();
            }
        });
    }

    static byte[] readBounded(InputStream input, int limit) throws IOException {
        ByteArrayOutputStream output = new ByteArrayOutputStream();
        byte[] buffer = new byte[8192];
        for (int count; (count = input.read(buffer)) != -1;) {
            if (output.size() + count > limit) throw new IOException("Response too large");
            output.write(buffer, 0, count);
        }
        return output.toByteArray();
    }

    private static String field(String boundary, String name, String value) {
        return "--" + boundary + "\r\nContent-Disposition: form-data; name=\"" + name + "\"\r\n\r\n" + value + "\r\n";
    }
}
