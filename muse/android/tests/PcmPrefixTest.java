package jp.sumi.stopwatch;

import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import java.util.Arrays;

public final class PcmPrefixTest {
    public static void main(String[] args) {
        for (int rate : new int[]{8000, 16000, 24000, 44100, 48000, 96000}) {
            var input = ByteBuffer.allocate(rate / 5 * 2).order(ByteOrder.LITTLE_ENDIAN);
            for (int i = 0; input.remaining() >= 2; i++)
                input.putShort((short) (Math.sin(i * 0.13) * 23456 + Math.cos(i * 0.44) * 5432));
            int converted = 0;
            for (int size : new int[]{rate / 5, rate / 5 + 2, input.capacity()}) {
                var audio = new SpeechBackend.Audio(input.array(), rate);
                var partial = new SpeechBackend.Audio(Arrays.copyOf(input.array(), size), rate);
                byte[] full = Pcm.to16k(audio);
                byte[] prefix = Pcm.prefixTo16k(partial);
                if (prefix.length == 0 || prefix.length >= full.length ||
                        !Arrays.equals(prefix, Arrays.copyOf(full, prefix.length))) {
                    throw new AssertionError("PCM prefix differs at " + rate + " Hz, " + size + " bytes");
                }
                byte[] growth = Pcm.prefixTo16k(partial, converted);
                if (!Arrays.equals(growth, Arrays.copyOfRange(full, converted, prefix.length))) {
                    throw new AssertionError("Incremental PCM differs at " + rate + " Hz, " + size + " bytes");
                }
                converted = prefix.length;
                byte[] tail = Pcm.tailTo16k(audio, prefix.length);
                if (!Arrays.equals(tail, Arrays.copyOfRange(full, prefix.length, full.length))) {
                    throw new AssertionError("PCM tail differs at " + rate + " Hz, " + size + " bytes");
                }
            }
        }
    }
}
