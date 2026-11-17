package jp.sumi.stopwatch;

import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;
import org.json.JSONTokener;

import java.io.ByteArrayOutputStream;
import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;

record ChatCatalog(List<ChatCatalog.Chat> all, Map<String, String> hidden) {
    record Chat(String id, String title) {}
    record Watch(int hash, int selected) {
        static Watch read(byte[] data) {
            if (data == null || data.length != 5) throw new IllegalArgumentException("Unexpected chat status");
            var reader = ByteBuffer.wrap(data).order(ByteOrder.LITTLE_ENDIAN);
            return new Watch(reader.getInt(), Byte.toUnsignedInt(reader.get()));
        }
    }

    static final int MAX_CHATS = 64, ID_BYTES = 36;
    static final String LEGACY_ID = "3622d9af-4010-4247-ac57-cd702e8bc9d3";
    static final Chat MAIN = new Chat("", "メインチャット");
    static final ChatCatalog DEFAULT = new ChatCatalog(List.of(new Chat(LEGACY_ID, "M5Stack会話用")), Map.of());
    static final int STATUS = 1, BEGIN = 2, CHUNK = 3, COMMIT = 4;

    ChatCatalog {
        all = List.copyOf(all);
        hidden = Collections.unmodifiableMap(new LinkedHashMap<>(hidden));
    }

    static boolean isSideId(String id) {
        return id != null && id.matches("[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}");
    }

    static ChatCatalog read(String json) throws JSONException {
        var root = new JSONObject(json);
        var hidden = new LinkedHashMap<String, String>();
        for (Chat row : readRows(root.getJSONArray("hidden"), false)) hidden.put(row.id(), row.title());
        return new ChatCatalog(readRows(root.getJSONArray("chats"), true), hidden);
    }

    String encode() throws JSONException {
        var disabled = new ArrayList<Chat>();
        hidden.forEach((id, title) -> disabled.add(new Chat(id, title)));
        return new JSONObject().put("chats", rows(all)).put("hidden", rows(disabled)).toString();
    }

    ChatCatalog refreshed(String reply) throws JSONException {
        var root = replyObject(reply);
        if (!Boolean.TRUE.equals(root.opt("complete"))) throw new JSONException("Incomplete catalog");
        List<Chat> latest = readRows(root.getJSONArray("chats"), true);
        var disabled = new LinkedHashMap<>(hidden);
        for (Chat chat : latest) if (disabled.containsKey(chat.id())) disabled.put(chat.id(), chat.title());
        return new ChatCatalog(latest, disabled);
    }

    static boolean matchesRequest(String reply, String requestId) {
        try {
            return requestId.equals(replyObject(reply).opt("request_id"));
        } catch (JSONException e) {
            return false;
        }
    }

    private static JSONObject replyObject(String reply) throws JSONException {
        String json = reply.strip();
        if (json.startsWith("```")) {
            int newline = json.indexOf('\n');
            if (newline < 0 || !json.endsWith("```") || newline + 1 > json.length() - 3)
                throw new JSONException("Incomplete JSON fence");
            json = json.substring(newline + 1, json.length() - 3).strip();
        }
        if (!json.startsWith("{") || !json.endsWith("}")) throw new JSONException("Expected chat catalog JSON");
        var parser = new JSONTokener(json);
        if (!(parser.nextValue() instanceof JSONObject root) || parser.nextClean() != 0)
            throw new JSONException("Expected chat catalog JSON");
        return root;
    }

    ChatCatalog edit(String id, boolean hide) {
        var disabled = new LinkedHashMap<>(hidden);
        if (hide) all.stream().filter(chat -> chat.id().equals(id)).findFirst()
                .ifPresent(chat -> disabled.put(id, chat.title()));
        else disabled.remove(id);
        var rows = new ArrayList<>(all);
        if (!hide) rows.removeIf(chat -> chat.id().equals(id));
        return new ChatCatalog(rows, disabled);
    }

    ChatCatalog show(String id) {
        var disabled = new LinkedHashMap<>(hidden);
        disabled.remove(id);
        return new ChatCatalog(all, disabled);
    }

    List<Chat> visible() {
        var result = new ArrayList<Chat>();
        result.add(MAIN);
        for (Chat chat : all) if (!hidden.containsKey(chat.id())) result.add(chat);
        return List.copyOf(result);
    }

    byte[] watchData() {
        List<Chat> visible = visible();
        var out = new ByteArrayOutputStream();
        out.write(1);
        out.write(visible.size() - 1);
        for (Chat chat : visible.subList(1, visible.size())) {
            out.writeBytes(chat.id().getBytes(StandardCharsets.US_ASCII));
            byte[] title = chat.title().getBytes(StandardCharsets.UTF_8);
            int length = Math.min(64, title.length);
            while (length < title.length && (title[length] & 0xc0) == 0x80) --length;
            out.write(length);
            out.write(title, 0, length);
        }
        return out.toByteArray();
    }

    static int hash(byte[] data) {
        int hash = 0x811c9dc5;
        for (byte value : data) hash = (hash ^ Byte.toUnsignedInt(value)) * 0x01000193;
        return hash;
    }

    static byte[] command(int op, int value) {
        return ByteBuffer.allocate(4).order(ByteOrder.LITTLE_ENDIAN)
                .put((byte) 0xa6).put((byte) op).putShort((short) value).array();
    }

    static byte[] part(byte[] data, int offset, int mtu) {
        int size = Math.min(data.length - offset, Math.min(128, mtu - 7));
        return ByteBuffer.allocate(4 + size).order(ByteOrder.LITTLE_ENDIAN)
                .put((byte) 0xa6).put((byte) CHUNK).putShort((short) offset).put(data, offset, size).array();
    }

    private static List<Chat> readRows(JSONArray input, boolean limited) throws JSONException {
        if (limited && input.length() > MAX_CHATS) throw new JSONException("Too many chats");
        var rows = new LinkedHashMap<String, Chat>();
        for (int i = 0; i < input.length(); i++) {
            var item = input.getJSONObject(i);
            Object rawId = item.opt("session_id");
            Object rawTitle = item.opt("title");
            if (!(rawId instanceof String id) || !(rawTitle instanceof String title))
                throw new JSONException("Missing chat ID or title");
            id = id.toLowerCase(Locale.ROOT);
            title = title.replaceAll("[\\p{Cc}\\p{Cf}]", " ").strip();
            if (!isSideId(id) || title.isBlank() || title.length() > 160 || rows.putIfAbsent(id, new Chat(id, title)) != null)
                throw new JSONException("Invalid or duplicated chat");
        }
        return List.copyOf(rows.values());
    }

    private static JSONArray rows(List<Chat> list) throws JSONException {
        var result = new JSONArray();
        for (Chat row : list) result.put(new JSONObject().put("session_id", row.id()).put("title", row.title()));
        return result;
    }
}
