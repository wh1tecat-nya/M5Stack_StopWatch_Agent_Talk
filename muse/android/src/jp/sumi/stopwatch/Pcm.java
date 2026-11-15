package jp.sumi.stopwatch;

import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import java.nio.charset.StandardCharsets;

final class Pcm {
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
        byte[] input = audio.pcm16();
        int rate = audio.sampleRate();
        if (rate < 8000 || rate > 96000 || input.length == 0 || (input.length & 1) != 0 ||
                input.length > rate * 240) throw new IllegalArgumentException("Invalid PCM");
        if (rate == 16000) return input;
        int samples = input.length / 2;
        int count = (int) Math.round(samples * 16000.0 / rate);
        if (count == 0) throw new IllegalArgumentException("Empty PCM");
        var source = ByteBuffer.wrap(input).order(ByteOrder.LITTLE_ENDIAN).asShortBuffer();
        ByteBuffer target = ByteBuffer.allocate(count * 2).order(ByteOrder.LITTLE_ENDIAN);
        double scale = rate / 16000.0;
        double cutoff = Math.min(1, 1 / scale) * 0.9;
        int period = 16000 / gcd(rate, 16000);
        int radius = 24;
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
        for (int i = 0; i < count; i++) {
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
