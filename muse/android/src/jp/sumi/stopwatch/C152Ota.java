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
    static final int INFO_EXTENDED = 1, MTU = 517;
    static final int NO_MEMORY = 0x83;
    static final int HEADER = 6;
    private static final int MAX_PACKET = 512, BLOCK = 8192;
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

    record Info(int slot, boolean confirmed, byte[] id, int error, boolean compressed) {
        static Info read(byte[] data) {
            int end = 3 + ID_BYTES;
            if (data == null || !((data.length == end && data[0] == 1) ||
                    (data.length == end + 1 && (data[0] == 2 || data[0] == 3))) ||
                    (data[1] & ~1) != 0 || (data[2] & ~1) != 0)
                throw new IllegalArgumentException("Unexpected C152 OTA status");
            int error = data.length == end ? 0 : Byte.toUnsignedInt(data[end]);
            return new Info(data[1], data[2] == 1, Arrays.copyOfRange(data, 3, end), error, data[0] == 3);
        }

        boolean matches(Image image, int oldSlot) {
            return confirmed && slot != oldSlot && Arrays.equals(id, image.id());
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
            int capacity = compressed ? MAX_PACKET : AudioPacket.MAX_PACKET;
            int length = Math.min(bytes.length - offset, Math.min(capacity, mtu - 3) - HEADER);
            if (offset < 0 || length <= 0) throw new IllegalArgumentException("Invalid BLE OTA payload range");
            return header(length, DATA, offset).put(bytes, offset, length).array();
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
