package jp.sumi.stopwatch;

import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import java.nio.charset.StandardCharsets;

record MusePair(long revision, boolean reserved, String device, String access, String refresh) {
    MusePair {
        if (!device.matches("homelink-[0-9a-f]{6}") || !validAccess(access) || !validRefresh(refresh))
            throw new IllegalArgumentException("Invalid Muse device credentials");
    }

    private static boolean validAccess(String value) {
        return printable(value) && value.length() >= 10 &&
                !value.startsWith("hatch_refresh:") && !value.startsWith("mgst_");
    }

    static boolean validRefresh(String value) {
        return printable(value) && !value.endsWith(":") && !value.startsWith("mgst_");
    }

    private static boolean printable(String value) {
        return !value.isEmpty() && value.length() <= MuseAccessSync.MAX_TOKEN &&
                value.chars().allMatch(c -> c >= '!' && c <= '~');
    }

    static MusePair read(byte[] frame) {
        if (frame.length < 14) throw new IllegalArgumentException("Short Muse pair");
        ByteBuffer in = ByteBuffer.wrap(frame).order(ByteOrder.LITTLE_ENDIAN);
        long revision = in.getLong();
        int reserved = Byte.toUnsignedInt(in.get());
        int device = Byte.toUnsignedInt(in.get());
        int access = Short.toUnsignedInt(in.getShort());
        int refresh = Short.toUnsignedInt(in.getShort());
        if (reserved > 1 || device + access + refresh != in.remaining())
            throw new IllegalArgumentException("Invalid Muse pair length");
        return new MusePair(revision, reserved == 1, ascii(in, device), ascii(in, access), ascii(in, refresh));
    }

    byte[] replacement(String nextAccess, String nextRefresh) {
        if (!validAccess(nextAccess) || !validRefresh(nextRefresh)) throw new IllegalArgumentException("Invalid pair");
        byte[] next = nextAccess.getBytes(StandardCharsets.US_ASCII);
        byte[] refresh = nextRefresh.getBytes(StandardCharsets.US_ASCII);
        try {
            return ByteBuffer.allocate(12 + next.length + refresh.length).order(ByteOrder.LITTLE_ENDIAN)
                    .putLong(revision).putShort((short) next.length).putShort((short) refresh.length)
                    .put(next).put(refresh).array();
        } finally {
            java.util.Arrays.fill(next, (byte) 0);
            java.util.Arrays.fill(refresh, (byte) 0);
        }
    }

    private static String ascii(ByteBuffer in, int length) {
        String text = new String(in.array(), in.position(), length, StandardCharsets.US_ASCII);
        in.position(in.position() + length);
        return text;
    }
}
