package jp.sumi.stopwatch;

import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.util.Arrays;
import java.util.HashMap;
import java.util.Map;
import java.util.UUID;

final class MuseWire {
    private static final int LIMIT = 512 * 1024;
    static final int EVENT_BYTES_MAX = 256 * 1024;
    private final Map<Long, Parts> partial = new HashMap<>();

    record Frame(long stream, int status, boolean end, byte[] body) {}
    private record Parts(byte[][] chunks, int[] progress) {}

    static final class EventLines {
        private final ByteArrayOutputStream line = new ByteArrayOutputStream();
        private boolean overflow;

        String append(byte value) {
            if (value == '\n') {
                String event = overflow || line.size() == 0 ? null : line.toString(StandardCharsets.UTF_8);
                line.reset();
                overflow = false;
                return event;
            }
            if (!overflow) {
                if (line.size() == EVENT_BYTES_MAX) overflow = true;
                else line.write(value);
            }
            return null;
        }
    }

    byte[] request(int stream, String path, String body, boolean subscribe) {
        Writer request = new Writer();
        request.text(1, "POST");
        request.text(2, path);
        header(request, "Content-Type", "application/json");
        header(request, "x-app-id", "hatch-web");
        header(request, "x-request-id", "sumi-" + UUID.randomUUID());
        if (subscribe) header(request, "accept", "application/x-ndjson");
        request.text(4, body);
        request.number(5, 1);
        Writer frame = new Writer();
        frame.number(1, stream);
        frame.message(2, request);
        Writer service = new Writer();
        service.message(2, frame);
        Writer transport = new Writer();
        transport.number(3, 1);
        transport.message(4, service);
        return transport.bytes();
    }

    private static void header(Writer request, String name, String value) {
        Writer header = new Writer();
        header.text(1, name);
        header.text(2, value);
        request.message(3, header);
    }

    Frame decode(byte[] packet) throws IOException {
        Reader fields = new Reader(packet);
        long id = 0;
        int index = 0, total = 1;
        byte[] part = {};
        while (fields.advance()) {
            switch (fields.field) {
                case 1 -> id = fields.number();
                case 2 -> index = fields.smallNumber();
                case 3 -> total = fields.smallNumber();
                case 4 -> part = fields.bytes();
                default -> fields.skip();
            }
        }
        if (total <= 0 || total > 16 || index < 0 || index >= total) throw new IOException("Invalid Noise transport frame");
        if (total > 1) {
            if (!partial.containsKey(id) && partial.size() >= 4) throw new IOException("Too many Noise fragments");
            int chunks = total;
            Parts group = partial.computeIfAbsent(id, ignored -> new Parts(new byte[chunks][], new int[2]));
            if (group.chunks.length != total || group.chunks[index] != null || group.progress[1] + part.length > LIMIT)
                throw new IOException("Invalid Noise fragments");
            group.chunks[index] = part;
            group.progress[1] += part.length;
            if (++group.progress[0] < total) return null;
            ByteArrayOutputStream out = new ByteArrayOutputStream(group.progress[1]);
            for (byte[] chunk : group.chunks) out.writeBytes(chunk);
            partial.remove(id);
            part = out.toByteArray();
        }
        fields = new Reader(part);
        byte[] message = {};
        while (fields.advance()) {
            if (fields.field == 1) message = fields.bytes();
            else fields.skip();
        }
        fields = new Reader(message);
        long stream = 0;
        int kind = 0;
        byte[] body = {};
        while (fields.advance()) {
            if (fields.field == 1) stream = fields.number();
            else if (fields.field >= 3 && fields.field <= 5) {
                kind = fields.field;
                body = fields.bytes();
            } else fields.skip();
        }
        if (kind == 5) return new Frame(stream, -1, true, new byte[0]);
        if (kind != 3 && kind != 4) return null;
        fields = new Reader(body);
        int status = 0;
        boolean end = false;
        byte[] data = {};
        while (fields.advance()) {
            if (kind == 3 && fields.field == 1) status = fields.smallNumber();
            else if (fields.field == (kind == 3 ? 3 : 1)) data = fields.bytes();
            else if (fields.field == (kind == 3 ? 4 : 2)) end = fields.number() != 0;
            else fields.skip();
        }
        return new Frame(stream, status, end, data);
    }

    private static final class Writer {
        final ByteArrayOutputStream out = new ByteArrayOutputStream();
        void varint(long value) {
            while ((value & ~127L) != 0) { out.write((int) value & 127 | 128); value >>>= 7; }
            out.write((int) value);
        }
        void number(int field, long value) { varint(field << 3); varint(value); }
        void message(int field, Writer child) { field(field, child.bytes()); }
        void text(int field, String value) { field(field, value.getBytes(StandardCharsets.UTF_8)); }
        void field(int field, byte[] value) {
            varint(field << 3 | 2);
            varint(value.length);
            out.writeBytes(value);
        }
        byte[] bytes() { return out.toByteArray(); }
    }

    private static final class Reader {
        private final byte[] data;
        private int offset, type;
        int field;

        Reader(byte[] data) { this.data = data; }
        boolean advance() throws IOException {
            if (offset == data.length) return false;
            long tag = varint();
            if (tag < 8 || tag > Integer.MAX_VALUE) throw new IOException("Invalid protobuf field");
            field = (int) (tag >>> 3);
            type = (int) (tag & 7);
            return true;
        }
        long number() throws IOException {
            if (type != 0) throw new IOException("Expected protobuf integer");
            return varint();
        }
        int smallNumber() throws IOException {
            long value = number();
            if (value < 0 || value > Integer.MAX_VALUE) throw new IOException("Invalid protobuf integer");
            return (int) value;
        }
        byte[] bytes() throws IOException {
            if (type != 2) throw new IOException("Expected protobuf bytes");
            int count = length();
            byte[] value = Arrays.copyOfRange(data, offset, offset + count);
            offset += count;
            return value;
        }
        void skip() throws IOException {
            switch (type) {
                case 0 -> varint();
                case 1 -> offset += 8;
                case 2 -> { int count = length(); offset += count; }
                case 5 -> offset += 4;
                default -> throw new IOException("Invalid protobuf wire type");
            }
            if (offset > data.length) throw new IOException("Truncated protobuf");
        }
        private int length() throws IOException {
            long count = varint();
            if (count < 0 || count > data.length - offset) throw new IOException("Truncated protobuf");
            return (int) count;
        }
        private long varint() throws IOException {
            long value = 0;
            for (int bits = 0; bits <= 63 && offset < data.length; bits += 7) {
                int next = data[offset++] & 255;
                if (bits == 63 && next > 1) break;
                value |= (long) (next & 127) << bits;
                if (next < 128) return value;
            }
            throw new IOException("Truncated protobuf integer");
        }
    }
}
