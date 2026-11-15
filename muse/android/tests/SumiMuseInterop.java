package jp.sumi.stopwatch;

import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.nio.charset.StandardCharsets;
import java.util.Base64;

final class SumiMuseInterop {
    public static void main(String[] args) throws Exception {
        checkEventLines();
        var in = new BufferedReader(new InputStreamReader(System.in));
        var enc = Base64.getEncoder();
        var dec = Base64.getDecoder();
        var noise = new MuseNoise();
        var wire = new MuseWire();
        System.out.println(enc.encodeToString(noise.start()));
        System.out.println(enc.encodeToString(noise.finish(dec.decode(in.readLine()))));
        System.out.println(enc.encodeToString(noise.encrypt(wire.request(2, "/chat/stream",
                "{\"message\":\"こんにちは\",\"output_modality\":\"text\",\"device\":\"mcu-wearable\"}", false))));
        MuseWire.Frame frame = wire.decode(noise.decrypt(dec.decode(in.readLine())));
        if (frame.stream() != 2 || frame.status() != 200 || !frame.end()) throw new AssertionError(frame);
        System.out.println(enc.encodeToString(frame.body()));
    }

    private static void checkEventLines() {
        var lines = new MuseWire.EventLines();
        String event = "{\"type\":\"event\",\"text\":\"" + "あ".repeat(32 * 1024) + "\"}";
        for (byte value : event.getBytes(StandardCharsets.UTF_8)) {
            if (lines.append(value) != null) throw new AssertionError("event split before newline");
        }
        if (!event.equals(lines.append((byte) '\n'))) throw new AssertionError("large event lost");
        for (int i = 0; i <= MuseWire.EVENT_BYTES_MAX; i++) lines.append((byte) 'x');
        if (lines.append((byte) '\n') != null) throw new AssertionError("oversized event accepted");
        lines.append((byte) '{');
        lines.append((byte) '}');
        if (!"{}".equals(lines.append((byte) '\n'))) throw new AssertionError("next event lost");
    }
}
