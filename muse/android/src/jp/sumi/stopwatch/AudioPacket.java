package jp.sumi.stopwatch;

import java.nio.ByteBuffer;
import java.nio.ByteOrder;

record AudioPacket(int type, int turn, int offset, byte[] data) {
    static final int HEADER = 9;
    static final int MAX_PACKET = 253;
    static final int VERSION = 1;
    static final int HELLO = 1, NOTE_BEGIN = 2, NOTE_PCM = 3, NOTE_END = 4, NOTE_ACK = 5;
    static final int REPLY_BEGIN = 6, REPLY_PCM = 7, REPLY_END = 8, CANCEL = 9, ERROR = 10, REPLY_STATUS = 11;
    static final int CAPTION = 12;
    static final int GENERATING = 13;
    static final int STREAM_LENGTH = -1, FEATURE_STREAM = 1;
    static final int MAX_REPLY = 120 * 32000;
    static final int MAX_NOTE = 15 * 32000;
    private static final byte[] EMPTY = {};

    AudioPacket(int type, int turn, int offset) {
        this(type, turn, offset, EMPTY);
    }

    byte[] encode() {
        return ByteBuffer.allocate(HEADER + data.length).order(ByteOrder.LITTLE_ENDIAN)
                .put((byte) type).putInt(turn).putInt(offset).put(data).array();
    }

    static AudioPacket decode(byte[] bytes) {
        if (bytes.length < HEADER || bytes.length > MAX_PACKET) throw new IllegalArgumentException("Invalid packet size");
        ByteBuffer buffer = ByteBuffer.wrap(bytes).order(ByteOrder.LITTLE_ENDIAN);
        int type = Byte.toUnsignedInt(buffer.get());
        int turn = buffer.getInt();
        int offset = buffer.getInt();
        byte[] data = new byte[buffer.remaining()];
        buffer.get(data);
        return new AudioPacket(type, turn, offset, data);
    }

    static int pcmCapacity(int mtu) {
        return Math.max(0, (Math.min(mtu - 3, MAX_PACKET) - HEADER) & ~1);
    }
}
