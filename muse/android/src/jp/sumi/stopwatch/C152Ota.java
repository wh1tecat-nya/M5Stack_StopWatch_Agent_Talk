package jp.sumi.stopwatch;

import java.io.ByteArrayOutputStream;
import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import java.nio.charset.StandardCharsets;
import java.util.Arrays;
import java.util.zip.Deflater;

final class C152Ota {
    static final int MAX_BYTES = 0x400000;
    static final int TAG = 0xa7, INFO = 1, BEGIN = 2, DATA = 3, FINISH = 4, ABORT = 5, BEGIN_COMPRESSED = 6;
    static final int BEGIN_WINDOW = 7, PROGRESS = 8;
    static final int INFO_WINDOW = 2, MTU = 517;
    static final int NO_MEMORY = 0x83;
    static final int BAD_PROGRESS = 0x84;
    static final int HEADER = 6;
    private static final int MAX_PACKET = 512, BLOCK = 8192, WINDOW_PACKETS = 24;
    private static final int SECTOR = 4096;
    private static final int ID_BYTES = 12, ELF_SHA_OFFSET = 0xb0, PROJECT_OFFSET = 0x50;

    record Image(byte[] data, byte[] id) {
        static Image read(byte[] data) {
            if (data.length < 2 * SECTOR || data.length > MAX_BYTES || data.length % SECTOR != 0 ||
                    Byte.toUnsignedInt(data[0]) != 0xe9 || data[12] != 9 || data[13] != 0 ||
                    Byte.toUnsignedInt(data[data.length - SECTOR]) != 0xe7)
                throw new IllegalArgumentException("Expected a signed ESP32-S3 image");
            var descriptor = ByteBuffer.wrap(data).order(ByteOrder.LITTLE_ENDIAN);
            byte[] project = "muse-gadget\0".getBytes(StandardCharsets.US_ASCII);
            if (descriptor.getInt(32) != 0xabcd5432 || !Arrays.equals(data, PROJECT_OFFSET,
                    PROJECT_OFFSET + project.length, project, 0, project.length))
                throw new IllegalArgumentException("Expected a Muse gadget application");
            return new Image(data, Arrays.copyOfRange(data, ELF_SHA_OFFSET, ELF_SHA_OFFSET + ID_BYTES));
        }
    }

    record Info(int slot, boolean confirmed, byte[] id, int error, int version) {
        static Info read(byte[] data) {
            int end = 3 + ID_BYTES;
            if (data == null || !((data.length == end && data[0] == 1) ||
                    (data.length == end + 1 && data[0] >= 2 && data[0] <= 4)) ||
                    (data[1] & ~1) != 0 || (data[2] & ~1) != 0)
                throw new IllegalArgumentException("Unexpected C152 OTA status");
            int error = data.length == end ? 0 : Byte.toUnsignedInt(data[end]);
            return new Info(data[1], data[2] == 1, Arrays.copyOfRange(data, 3, end), error, data[0]);
        }

        boolean compressed() { return version >= 3; }
        boolean windowed() { return version >= 4; }

        boolean matches(Image image, int oldSlot) {
            return confirmed && slot != oldSlot && Arrays.equals(id, image.id());
        }
    }

    record Progress(int received, int error) {
        static Progress read(byte[] data, int confirmed, int windowEnd) {
            if (data == null || data.length != HEADER + 1 || Byte.toUnsignedInt(data[0]) != TAG || data[1] != PROGRESS)
                throw new IllegalArgumentException("Unexpected C152 OTA progress");
            int received = ByteBuffer.wrap(data).order(ByteOrder.LITTLE_ENDIAN).getInt(2);
            if (received < confirmed || received > windowEnd)
                throw new IllegalArgumentException("C152 OTA progress outside the current window");
            return new Progress(received, Byte.toUnsignedInt(data[HEADER]));
        }
    }

    record Upload(byte[] bytes, boolean compressed) {
        static Upload prepare(Image image, boolean compression) {
            byte[] raw = image.data();
            if (!compression) return new Upload(raw, false);
            var wire = new ByteArrayOutputStream(raw.length);
            var deflater = new Deflater();
            byte[] block = new byte[BLOCK];
            try {
                for (int offset = 0; offset < raw.length; offset += BLOCK) {
                    int length = Math.min(BLOCK, raw.length - offset);
                    deflater.reset();
                    deflater.setInput(raw, offset, length);
                    deflater.finish();
                    int encoded = deflater.deflate(block);
                    boolean smaller = deflater.finished() && encoded > 0 && encoded < length;
                    int payload = smaller ? encoded : length;
                    wire.write(payload & 255);
                    wire.write(payload >> 8);
                    wire.write(smaller ? block : raw, smaller ? 0 : offset, payload);
                }
            } finally {
                deflater.end();
            }
            return wire.size() < raw.length ? new Upload(wire.toByteArray(), true) : new Upload(raw, false);
        }

        byte[] part(int offset, int mtu) {
            return part(offset, mtu, bytes.length);
        }

        byte[] part(int offset, int mtu, int end) {
            int length = Math.min(end - offset, capacity(mtu));
            if (offset < 0 || end > bytes.length || length <= 0)
                throw new IllegalArgumentException("Invalid BLE OTA payload range");
            return header(length, DATA, offset).put(bytes, offset, length).array();
        }

        int windowEnd(int offset, int mtu) {
            return Math.min(bytes.length, offset + capacity(mtu) * WINDOW_PACKETS);
        }

        private int capacity(int mtu) {
            int capacity = compressed ? MAX_PACKET : AudioPacket.MAX_PACKET;
            return Math.min(capacity, mtu - 3) - HEADER;
        }
    }

    static byte[] command(int kind, int value) {
        return header(0, kind, value).array();
    }

    private static ByteBuffer header(int length, int kind, int value) {
        return ByteBuffer.allocate(HEADER + length).order(ByteOrder.LITTLE_ENDIAN)
                .put((byte) TAG).put((byte) kind).putInt(value);
    }

    private C152Ota() {}
}
