package jp.sumi.stopwatch;

import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import java.util.Arrays;

final class MuseAccessSyncTest {
    private static void reject(MuseAccessSync.Download download, byte[] packet) {
        try {
            download.append(packet);
            throw new AssertionError("Accepted an invalid credential chunk");
        } catch (IllegalArgumentException expected) {
            // A rejected chunk must never advance the download.
        }
    }

    public static void main(String[] args) {
        byte[] token = new byte[MuseAccessSync.MAX_TOKEN];
        Arrays.fill(token, (byte) 'x');
        for (int mtu : new int[]{23, 256, 517}) {
            var download = new MuseAccessSync.Download();
            reject(download, new byte[]{0, 8, 1}); // 2048 is one byte over the limit.
            while (!download.finished()) {
                int offset = download.offset();
                int amount = Math.min(token.length - offset, Math.min(128, mtu - 4));
                byte[] read = ByteBuffer.allocate(2 + amount).order(ByteOrder.LITTLE_ENDIAN)
                        .putShort((short) token.length).put(token, offset, amount).array();
                download.append(read);
                reject(download, new byte[]{20, 0, 1}); // The total changed across reads.
            }
            if (!Arrays.equals(token, download.token())) throw new AssertionError("Download changed the token");
            download.clear();
            if (!Arrays.equals(new byte[token.length], download.token())) throw new AssertionError("Download not cleared");

            byte[] payload = Arrays.copyOf(token, token.length + 32);
            ByteBuffer received = ByteBuffer.allocate(payload.length);
            while (received.hasRemaining()) {
                byte[] part = MuseAccessSync.part(mtu, received.position(), payload);
                ByteBuffer in = ByteBuffer.wrap(part).order(ByteOrder.LITTLE_ENDIAN);
                if (part.length > mtu - 3 || in.get() != (byte) 0xa5 || in.get() != MuseAccessSync.WRITE ||
                        Short.toUnsignedInt(in.getShort()) != received.position())
                    throw new AssertionError("Invalid upload frame");
                received.put(in);
            }
            if (!Arrays.equals(payload, received.array())) throw new AssertionError("Upload changed the payload");
        }
        byte[] id = "homelink-123abc".getBytes(java.nio.charset.StandardCharsets.US_ASCII);
        byte[] refresh = "hatch_refresh:fixture".getBytes(java.nio.charset.StandardCharsets.US_ASCII);
        long revision = 0xa1b2c3d400431237L;
        byte[] pairFrame = ByteBuffer.allocate(14 + id.length + token.length + refresh.length)
                .order(ByteOrder.LITTLE_ENDIAN).putLong(revision).put((byte) 1).put((byte) id.length)
                .putShort((short) token.length).putShort((short) refresh.length).put(id).put(token).put(refresh).array();
        var download = new MuseAccessSync.Download(MuseAccessSync.MAX_PAIR);
        while (!download.finished()) {
            int offset = download.offset();
            int length = Math.min(128, pairFrame.length - offset);
            download.append(ByteBuffer.allocate(2 + length).order(ByteOrder.LITTLE_ENDIAN)
                    .putShort((short) pairFrame.length).put(pairFrame, offset, length).array());
        }
        MusePair pair = MusePair.read(download.token());
        if (!pair.reserved() || pair.revision() != revision || !pair.device().equals("homelink-123abc") ||
                !pair.refresh().equals("hatch_refresh:fixture") || pair.access().length() != token.length)
            throw new AssertionError("Pair was not reconstructed");
        byte[] reserve = MuseAccessSync.reservation(MuseAccessSync.RESERVE, pair.revision());
        if (reserve.length != 12 || ByteBuffer.wrap(reserve).order(ByteOrder.LITTLE_ENDIAN).getLong(4) != revision)
            throw new AssertionError("Wrong reservation revision");
        ByteBuffer replacement = ByteBuffer.wrap(pair.replacement("new-access-synthetic", "new-refresh-synthetic"))
                .order(ByteOrder.LITTLE_ENDIAN);
        if (replacement.getLong() != revision || replacement.getShort() != "new-access-synthetic".length() ||
                replacement.getShort() != "new-refresh-synthetic".length())
            throw new AssertionError("The commit must use the old revision with both new tokens");
        pairFrame[8] = 2;
        try {
            MusePair.read(pairFrame);
            throw new AssertionError("Accepted an invalid reservation state");
        } catch (IllegalArgumentException expected) {
            // Unsupported state must not be used to refresh credentials.
        }
    }
}
