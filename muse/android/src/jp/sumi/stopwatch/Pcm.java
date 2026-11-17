package jp.sumi.stopwatch;

import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import java.nio.charset.StandardCharsets;
import java.util.Arrays;

final class Pcm {
    private static final int FILTER_RADIUS = 24;
    static byte[] waveHeader(int bytes, int rate) {
        ByteBuffer out = ByteBuffer.allocate(44).order(ByteOrder.LITTLE_ENDIAN);
        out.put("RIFF".getBytes(StandardCharsets.US_ASCII)).putInt(36 + bytes);
        out.put("WAVEfmt ".getBytes(StandardCharsets.US_ASCII)).putInt(16);
        out.putShort((short) 1).putShort((short) 1).putInt(rate).putInt(rate * 2);
        out.putShort((short) 2).putShort((short) 16);
        out.put("data".getBytes(StandardCharsets.US_ASCII)).putInt(bytes);
        return out.array();
    }

    static byte[] to16k(SpeechBackend.Audio audio) {
        return tailTo16k(audio, 0);
    }

    static byte[] prefixTo16k(SpeechBackend.Audio audio) {
        return prefixTo16k(audio, 0);
    }

    static byte[] prefixTo16k(SpeechBackend.Audio audio, int convertedBytes) {
        return to16k(audio, true, convertedBytes);
    }

    static byte[] tailTo16k(SpeechBackend.Audio audio, int convertedBytes) {
        return to16k(audio, false, convertedBytes);
    }

    private static byte[] to16k(SpeechBackend.Audio audio, boolean prefix, int convertedBytes) {
        byte[] input = audio.pcm16();
        int rate = audio.sampleRate();
        if (rate < 8000 || rate > 96000 || input.length == 0 || (input.length & 1) != 0 ||
                input.length > rate * 240 || convertedBytes < 0 || (convertedBytes & 1) != 0)
            throw new IllegalArgumentException("Invalid PCM");
        int samples = input.length / 2;
        // Hold the samples whose filter would need audio beyond the prefix. They
        // become the start of the tail when the full utterance is available.
        int count = rate == 16000 ? samples - (prefix && samples > 1 ? 1 : 0) :
                prefix ? (int) Math.ceil((samples - FILTER_RADIUS) * 16000.0 / rate) :
                (int) Math.round(samples * 16000.0 / rate);
        int start = convertedBytes / 2;
        if (count <= 0 || start > count) throw new IllegalArgumentException("Invalid PCM range");
        if (rate == 16000) return start == 0 && count == samples ? input :
                Arrays.copyOfRange(input, convertedBytes, count * 2);
        if (start == count) return new byte[0];
        var source = ByteBuffer.wrap(input).order(ByteOrder.LITTLE_ENDIAN).asShortBuffer();
        ByteBuffer target = ByteBuffer.allocate((count - start) * 2).order(ByteOrder.LITTLE_ENDIAN);
        double scale = rate / 16000.0;
        double cutoff = Math.min(1, 1 / scale) * 0.9;
        int period = 16000 / gcd(rate, 16000);
        int radius = FILTER_RADIUS;
        double[][] filter = new double[period][2 * radius + 1];
        for (int phase = 0; phase < period; phase++) {
            double fraction = (phase * scale) % 1;
            for (int tap = -radius; tap <= radius; tap++) {
                double t = fraction - tap;
                double sinc = Math.abs(t) < 1e-10 ? cutoff : Math.sin(Math.PI * cutoff * t) / (Math.PI * t);
                double window = Math.abs(t) > radius ? 0 : 0.5 + 0.5 * Math.cos(Math.PI * t / radius);
                filter[phase][tap + radius] = sinc * window;
            }
        }
        for (int i = start; i < count; i++) {
            int center = (int) ((long) i * rate / 16000);
            double sum = 0, weight = 0;
            double[] coefficients = filter[i % period];
            for (int tap = -radius; tap <= radius; tap++) {
                int index = center + tap;
                if (index < 0 || index >= samples) continue;
                double coeff = coefficients[tap + radius];
                sum += source.get(index) * coeff;
                weight += coeff;
            }
            long value = Math.round(sum / weight);
            target.putShort((short) Math.max(Short.MIN_VALUE, Math.min(Short.MAX_VALUE, value)));
        }
        return target.array();
    }

    private static int gcd(int a, int b) {
        while (b != 0) { int remainder = a % b; a = b; b = remainder; }
        return a;
    }

    private Pcm() {}
}
