package jp.sumi.stopwatch;

import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import java.util.Arrays;

final class MuseAccessSync {
    static final int READ = 1, BEGIN_WRITE = 2, WRITE = 3, COMMIT = 4, CLEAR = 5;
    static final int PAIR_READ = 6, PAIR_BEGIN = 7, RESERVE = 8, RELEASE = 9;
    static final int MAX_TOKEN = 2047;
    static final int MAX_PAIR = 14 + 31 + 2 * MAX_TOKEN;
    static final int HEADER = 4;
    private static final int CHUNK = 128;
    private static final byte TAG = (byte) 0xa5;

    static byte[] command(int operation, int value) {
        return ByteBuffer.allocate(HEADER).order(ByteOrder.LITTLE_ENDIAN)
                .put(TAG).put((byte) operation).putShort((short) value).array();
    }

    static byte[] reservation(int operation, long revision) {
        return ByteBuffer.allocate(HEADER + Long.BYTES).order(ByteOrder.LITTLE_ENDIAN)
                .put(TAG).put((byte) operation).putShort((short) 0).putLong(revision).array();
    }

    static byte[] part(int mtu, int offset, byte[] payload) {
        int length = Math.min(payload.length - offset, Math.min(CHUNK, mtu - 3 - HEADER));
        return ByteBuffer.allocate(HEADER + length).order(ByteOrder.LITTLE_ENDIAN)
                .put(TAG).put((byte) WRITE).putShort((short) offset).put(payload, offset, length).array();
    }

    static final class Download {
        private final int maximum;
        private byte[] token;
        private int offset;

        Download() { this(MAX_TOKEN); }
        Download(int maximum) { this.maximum = maximum; }

        int maximum() { return maximum; }
        int offset() { return offset; }
        boolean finished() { return token != null && offset == token.length; }
        byte[] token() { return token; }

        void append(byte[] packet) {
            if (packet == null || packet.length < 3 || packet.length > CHUNK + 2)
                throw new IllegalArgumentException("Invalid Muse access chunk");
            int length = (packet[0] & 255) | ((packet[1] & 255) << 8);
            if (length < 10 || length > maximum || (token != null && length != token.length) ||
                    packet.length - 2 > length - offset)
                throw new IllegalArgumentException("Invalid Muse access length");
            if (token == null) token = new byte[length];
            System.arraycopy(packet, 2, token, offset, packet.length - 2);
            offset += packet.length - 2;
        }

        void clear() {
            if (token != null) Arrays.fill(token, (byte) 0);
        }
    }

    private MuseAccessSync() {}
}
