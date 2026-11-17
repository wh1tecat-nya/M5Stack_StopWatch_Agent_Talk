package jp.sumi.stopwatch;

import java.io.ByteArrayOutputStream;
import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import java.util.Arrays;
import java.util.Random;
import java.util.zip.Inflater;

public final class C152OtaTest {
    public static void main(String[] args) throws Exception {
        byte[] raw = new byte[8192 * 2 + 4096];
        new Random(152).nextBytes(raw);
        Arrays.fill(raw, 0, 8192, (byte)10);
        Arrays.fill(raw, 8192 * 2, raw.length, (byte)25);
        var image = new C152Ota.Image(raw, new byte[12]);
        var legacy = C152Ota.Upload.prepare(image, false);
        check(!legacy.compressed() && legacy.part(0, 517).length == 253, "Legacy packet size");
        byte[] incompressible = new byte[8192];
        new Random(15).nextBytes(incompressible);
        check(!C152Ota.Upload.prepare(new C152Ota.Image(incompressible, new byte[12]), true).compressed(),
                "Avoid expanding a firmware image");
        var fast = C152Ota.Upload.prepare(image, true);
        check(fast.compressed() && fast.bytes().length < raw.length, "Compression");
        check(fast.part(0, 517).length == 512, "Extended packet size");
        check(fast.part(0, 256).length == 253, "Negotiated MTU");

        var received = new ByteArrayOutputStream();
        for (int offset = 0; offset < fast.bytes().length;) {
            var packet = ByteBuffer.wrap(fast.part(offset, 517)).order(ByteOrder.LITTLE_ENDIAN);
            check(Byte.toUnsignedInt(packet.get()) == C152Ota.TAG && packet.get() == C152Ota.DATA &&
                    packet.getInt() == offset, "Packet type and offset");
            received.write(packet.array(), packet.position(), packet.remaining());
            offset += packet.remaining();
        }
        var wire = ByteBuffer.wrap(received.toByteArray()).order(ByteOrder.LITTLE_ENDIAN);
        var output = new ByteArrayOutputStream();
        for (int offset = 0; offset < raw.length; offset += 8192) {
            int blockSize = Math.min(8192, raw.length - offset);
            int length = Short.toUnsignedInt(wire.getShort());
            byte[] payload = new byte[length];
            wire.get(payload);
            if (length == blockSize) output.write(payload);
            else {
                var inflater = new Inflater();
                try {
                    inflater.setInput(payload);
                    byte[] block = new byte[blockSize];
                    check(inflater.inflate(block) == blockSize && inflater.finished() &&
                            inflater.getRemaining() == 0, "Block length and checksum");
                    output.write(block);
                } finally {
                    inflater.end();
                }
            }
        }
        check(!wire.hasRemaining() && Arrays.equals(raw, output.toByteArray()), "Image is byte-identical");

        byte[] status = new byte[16];
        status[0] = 2;
        check(!C152Ota.Info.read(status).compressed(), "Legacy capability");
        status[0] = 3;
        check(C152Ota.Info.read(status).compressed(), "Compressed transfer capability");
        check(!C152Ota.Info.read(status).windowed(), "Compressed-only firmware remains compatible");
        status[0] = 4;
        check(C152Ota.Info.read(status).compressed() && C152Ota.Info.read(status).windowed(), "Window capability");
        check(new C152Ota.Upload(new byte[25000], true).windowEnd(1000, 517) == 1000 + 24 * 506,
                "Window respects the negotiated payload size");
        check(fast.windowEnd(0, 517) == fast.bytes().length, "Final window is bounded by the file");
        check(fast.part(0, 517, 100).length == C152Ota.HEADER + 100, "Packet is bounded by the window");
        byte[] progress = Arrays.copyOf(C152Ota.command(C152Ota.PROGRESS, 17), 7);
        check(C152Ota.Progress.read(progress, 16, 24).received() == 17, "Partial acknowledgement");
        progress[6] = (byte) 0x82;
        check(C152Ota.Progress.read(progress, 16, 24).error() == 0x82, "Unsigned error");
        rejectProgress(progress, 18, 24);
        rejectProgress(progress, 0, 16);
        rejectProgress(Arrays.copyOf(progress, 6), 0, 24);
    }

    private static void rejectProgress(byte[] data, int confirmed, int end) {
        try {
            C152Ota.Progress.read(data, confirmed, end);
        } catch (IllegalArgumentException expected) {
            return;
        }
        throw new AssertionError("Invalid progress accepted");
    }

    private static void check(boolean valid, String message) {
        if (!valid) throw new AssertionError(message);
    }
}
