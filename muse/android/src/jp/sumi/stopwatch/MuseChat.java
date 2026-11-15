package jp.sumi.stopwatch;

import android.util.Log;
import org.json.JSONException;
import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.net.URI;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.concurrent.ExecutorService;
import java.util.function.Supplier;

final class MuseChat implements SpeechBackend.Chat {
    private static final ConversationLog LOG = ConversationLog.INSTANCE;
    private static final String SIDE_CHAT = "3622d9af-4010-4247-ac57-cd702e8bc9d3";
    private static final long SETTLE_MS = 3000;
    private static final int TEXT_BYTES_MAX = 32768;
    private static final int PENDING_BYTES_MAX = 2 * MuseWire.EVENT_BYTES_MAX;
    private final Supplier<String> credential;
    private final ExecutorService workers;

    private record Vm(String id, String token) {}
    private record Settings(String auth, String preferredVm) {}

    MuseChat(Supplier<String> credential, ExecutorService workers) {
        this.credential = credential;
        this.workers = workers;
    }

    @Override public SpeechBackend.Task reply(SpeechBackend.ChatRequest call, SpeechBackend.Result<String> reply) {
        return run(call, reply);
    }

    SpeechBackend.Task check(SpeechBackend.Result<String> result) {
        return run(null, result);
    }

    private SpeechBackend.Task run(SpeechBackend.ChatRequest call, SpeechBackend.Result<String> reply) {
        String auth = credential.get();
        return NetworkTask.start(workers, reply, "Museとの通信に失敗しました", request -> {
            if (auth.isBlank()) throw new NetworkTask.Failure("スマホの会話設定でMuse端末の認証情報を設定してください");
            JSONObject settings = new JSONObject(auth);
            Vm vm;
            try {
                vm = vm(request, new Settings(settings.getString("token"), settings.optString("vm")));
            } catch (Exception e) {
                throw stageError("アカウント確認", e);
            }
            return chat(request, vm, call);
        });
    }

    private static Vm vm(NetworkTask request, Settings credentials) throws Exception {
        if (credentials.auth.startsWith("mgst_"))
            throw new NetworkTask.Failure("SDKトークンは端末登録用です。Muse端末の認証情報を設定してください");
        if (!isAccessToken(credentials.auth))
            throw new NetworkTask.Failure("Museの端末アクセストークンを確認してください");
        var http = request.open(URI.create("https://api.muse.ai/fetch_vms"));
        http.setRequestProperty("Authorization", "Bearer " + credentials.auth);
        http.setRequestProperty("X-API-Version", "1.0.0");
        int code = http.getResponseCode();
        LOG.event(ConversationLog.MUSE, "GET /fetch_vms · HTTP " + code);
        if (code == 401 || code == 403)
            throw new NetworkTask.Failure("Museが端末アクセストークンを拒否しました（HTTP " + code + "）");
        if (code != 200) throw new NetworkTask.Failure("Museの接続先を取得できません（HTTP " + code + "）");
        JSONObject body;
        try (var input = http.getInputStream()) {
            body = new JSONObject(new String(OpenAiAsr.readBounded(input, 1 << 20), StandardCharsets.UTF_8));
        }
        var list = body.optJSONArray("vm_list");
        Vm fallback = null;
        if (list != null) for (int i = 0; i < list.length(); i++) {
            JSONObject item = list.getJSONObject(i);
            String token = item.optString("vm_auth_token");
            String id = item.optString("vm_id");
            if (id.isBlank()) {
                String url = item.optString("vm_ws_url", item.optString("vm_url"));
                if (!url.isBlank()) {
                    URI uri = URI.create(url);
                    String host = uri.getHost();
                    if (host != null) id = host.split("\\.", 2)[0];
                }
            }
            if (token.isBlank() || id.isBlank()) continue;
            Vm vm = new Vm(id, token);
            if (!credentials.preferredVm.isBlank()) {
                if (credentials.preferredVm.equals(id)) return vm;
                continue;
            }
            if (item.optBoolean("default")) return vm;
            if (fallback == null) fallback = vm;
        }
        if (fallback == null) throw new NetworkTask.Failure("選択したMuse VMを取得できません");
        return fallback;
    }

    static boolean isAccessToken(String value) {
        return !value.startsWith("mgst_") && !value.startsWith("hatch_refresh:") && value.matches("[!-~]{10,16384}");
    }

    private static String chat(NetworkTask request, Vm vm, SpeechBackend.ChatRequest call) throws Exception {
        if (call != null && call.text().getBytes(StandardCharsets.UTF_8).length > 8192)
            throw new NetworkTask.Failure("文字起こしが長すぎます");
        String stage = "VM接続";
        try (MuseSocket socket = new MuseSocket(vm.id, vm.token, request)) {
            stage = "暗号化接続";
            MuseNoise noise = new MuseNoise();
            MuseWire wire = new MuseWire();
            socket.send(noise.start());
            long begin = now();
            byte[] handshake;
            while ((handshake = socket.receive()) == null && now() - begin < 12000) {}
            if (handshake == null) throw new IOException("Muse handshake timed out");
            socket.send(noise.finish(handshake));
            LOG.event(ConversationLog.MUSE, "VM · 暗号化接続完了");
            if (call == null) return "Muse VMに接続できました";

            stage = "会話先の購読";
            String session = call.chat() == 1 ? SIDE_CHAT : "";
            JSONObject scope = new JSONObject();
            if (!session.isEmpty()) scope.put("session_id", session);
            socket.send(noise.encrypt(wire.request(1, "/chat/subscribe", scope.toString(), true)));
            MuseWire.Frame first;
            while (true) {
                if (now() - begin > 18000) throw new NetworkTask.Failure("Museの会話先を購読できません");
                MuseWire.Frame frame = receive(socket, noise, wire);
                if (frame == null || frame.stream() != 1) continue;
                if (frame.status() == 0 && !frame.end()) continue;
                if (frame.status() < 200 || frame.status() >= 300 || frame.end())
                    throw new NetworkTask.Failure("Museの会話先を購読できません");
                first = frame;
                break;
            }
            stage = "メッセージ送信";
            JSONObject post = new JSONObject(scope.toString()).put("message", call.text()).put("output_modality", "text");
            if (call.wearable()) post.put("device", "mcu-wearable");
            socket.send(noise.encrypt(wire.request(2, "/chat/stream", post.toString(), false)));
            LOG.body(ConversationLog.MUSE, "送信 · POST /chat/stream", call.text());
            stage = "返答の受信";
            return reply(socket, noise, wire, session, first);
        } catch (Exception e) {
            throw stageError(stage, e);
        }
    }

    private static NetworkTask.Failure stageError(String stage, Exception error) {
        if (error instanceof NetworkTask.Failure failure) return failure;
        var source = error.getStackTrace();
        LOG.event(ConversationLog.MUSE, stage + " · " + error.getClass().getSimpleName());
        String origin = source.length == 0 ? "" : " at " + source[0];
        Log.w("SumiMuse", stage + " failed: " + error.getClass().getSimpleName() + origin);
        return new NetworkTask.Failure("Museとの通信に失敗しました（" + stage + "）");
    }

    private static final class Message {
        final StringBuilder text = new StringBuilder();
        boolean done;
    }

    private static final class ReplyBinding {
        final HashSet<String> userIds = new HashSet<>();
        long lastUserSequence;
        boolean lastUserIsOurs;
        boolean busy;
        boolean taskFinished;

        void userMessage(String id, long sequence) {
            if (sequence > lastUserSequence) {
                lastUserSequence = sequence;
                lastUserIsOurs = userIds.contains(id);
            }
        }

        boolean followsOurMessage(long sequence) {
            return lastUserIsOurs && sequence > lastUserSequence;
        }
    }

    private static String reply(MuseSocket socket, MuseNoise noise, MuseWire wire, String session,
                                MuseWire.Frame frame) throws Exception {
        ByteArrayOutputStream ack = new ByteArrayOutputStream();
        MuseWire.EventLines lines = new MuseWire.EventLines();
        ArrayList<JSONObject> pending = new ArrayList<>();
        ReplyBinding binding = new ReplyBinding();
        LinkedHashMap<String, Message> messages = new LinkedHashMap<>();
        boolean accepted = false;
        int pendingBytes = 0;
        long after = now() + 60000, last = now();
        while (now() < after) {
            if (accepted && !binding.busy && !messages.isEmpty() &&
                    messages.values().stream().allMatch(m -> m.done) &&
                    (binding.taskFinished || now() - last >= SETTLE_MS))
                return assemble(messages);
            if (frame == null) {
                frame = receive(socket, noise, wire);
                if (frame == null) continue;
            }
            if (frame.stream() == 2) {
                if (frame.status() != 0 && (frame.status() < 200 || frame.status() >= 300))
                    throw new NetworkTask.Failure("Museへのテキスト送信に失敗しました");
                if (ack.size() + frame.body().length > TEXT_BYTES_MAX) throw new IOException("Muse acknowledgement too large");
                ack.writeBytes(frame.body());
                if (frame.end()) {
                    JSONObject json = new JSONObject(ack.toString(StandardCharsets.UTF_8));
                    JSONObject object = json.optJSONObject("result");
                    if (object == null) object = json;
                    if (!session.isEmpty() && (!session.equals(object.optString("session_id")) || !object.optBoolean("is_thread")))
                        throw new NetworkTask.Failure("Museの送信先が一致しません");
                    if (session.isEmpty() && object.optBoolean("is_thread"))
                        throw new NetworkTask.Failure("Museの送信先がメインチャットではありません");
                    for (String key : new String[]{"message_id", "reply_to_message_id"}) {
                        String value = string(object, key);
                        if (!value.isBlank()) binding.userIds.add(value);
                    }
                    if (binding.userIds.isEmpty()) throw new IOException("No Muse message id");
                    accepted = true;
                    for (JSONObject event : pending) event(event, session, binding, messages);
                    pending.clear();
                    last = now();
                }
            } else if (frame.stream() == 1) {
                if (frame.status() < 0 || frame.end()) throw new IOException("Muse subscription closed");
                for (byte value : frame.body()) {
                    String json = lines.append(value);
                    if (json == null || json.isBlank()) continue;
                    JSONObject event;
                    try { event = new JSONObject(json); }
                    catch (JSONException malformed) { continue; }
                    if (accepted) {
                        if (event(event, session, binding, messages)) last = now();
                    } else if (relevant(event, session)) {
                        pendingBytes += json.getBytes(StandardCharsets.UTF_8).length;
                        if (pending.size() >= 128 || pendingBytes > PENDING_BYTES_MAX)
                            throw new IOException("Too many unacknowledged Muse events");
                        pending.add(event);
                    }
                }
            }
            frame = null;
        }
        if (accepted && !messages.isEmpty() && messages.values().stream().allMatch(m -> m.done)) return assemble(messages);
        throw new NetworkTask.Failure("Museの返答がタイムアウトしました");
    }

    private static String assemble(LinkedHashMap<String, Message> messages) {
        ArrayList<String> result = new ArrayList<>();
        for (Message message : messages.values()) {
            String raw = message.text.toString().strip();
            if (!raw.isEmpty()) LOG.body(ConversationLog.MUSE, "受信 · アシスタント返答", raw);
            String body = MuseReplyText.withoutContextStatus(raw);
            if (body.length() < raw.length()) LOG.event(ConversationLog.MUSE,
                    "容量表示を除去 · " + raw.codePointCount(0, raw.length()) + " → " +
                            body.codePointCount(0, body.length()) + "文字");
            if (!body.isEmpty()) result.add(body);
        }
        return String.join("\n", result);
    }

    private static boolean relevant(JSONObject record, String session) {
        if (!"event".equals(record.optString("type"))) return false;
        JSONObject data = record.optJSONObject("payload");
        if (data == null) return false;
        String actual = string(data, "session_id");
        if (actual.isEmpty()) actual = string(record, "session_id");
        if (!session.equals(actual)) return false;
        return switch (record.optString("event")) {
            case "agent.status", "task.status", "message.user", "delta.text_append", "message.assistant",
                    "delta.message_done", "delta.message_start" -> true;
            default -> false;
        };
    }

    private static boolean event(JSONObject record, String session, ReplyBinding binding,
                                 LinkedHashMap<String, Message> messages) throws Exception {
        if (!relevant(record, session)) return false;
        JSONObject data = record.optJSONObject("payload");
        String type = record.optString("event");
        long sequence = record.optLong("seq");
        String id = string(data, "message_id");
        if (id.isEmpty()) id = string(record, "message_id");
        if (id.isEmpty()) id = string(data, "id");
        if ("message.user".equals(type)) {
            binding.userMessage(id, sequence);
            return false;
        }
        String parent = string(data, "reply_to_message_id");
        if (parent.isEmpty()) parent = string(data, "parent_message_id");
        boolean linked = binding.userIds.contains(parent) || messages.containsKey(parent);
        if ("agent.status".equals(type) || "task.status".equals(type)) {
            if (!parent.isEmpty() && !linked) return false;
            String activity = data.optString("activity_code");
            String status = data.optString("status");
            boolean terminal = status.equals("completed") || status.equals("failed");
            if ("task.status".equals(type) && !status.isEmpty()) {
                binding.taskFinished = terminal && (linked || binding.userIds.contains(id) || messages.containsKey(id));
            }
            if (!activity.isEmpty()) binding.busy = !activity.equals("online") && !activity.equals("idle");
            else if (!status.isEmpty()) binding.busy = !terminal && !status.equals("idle");
            return true;
        }
        boolean append = "delta.text_append".equals(type);
        boolean full = "message.assistant".equals(type);
        boolean done = "delta.message_done".equals(type) || full;
        if (id.isEmpty() || "user".equals(string(data, "role"))) return false;
        Message message = messages.get(id);
        if (message == null) {
            boolean unnamed = !session.isEmpty() && (parent.isEmpty() || id.equals(parent));
            if (!linked && !(unnamed && binding.followsOurMessage(sequence))) return false;
            if (messages.size() >= 8) throw new IOException("Too many Muse replies");
            message = new Message();
            messages.put(id, message);
        }
        if (message.done) return false;
        if (append) message.text.append(string(data, "text"));
        if (done && (!full || data.optBoolean("display_text_ready", true))) {
            String text = finalText(data, id);
            if (!text.isBlank()) {
                message.text.setLength(0);
                message.text.append(text);
            }
            message.done = true;
        }
        if (message.text.length() > TEXT_BYTES_MAX) throw new IOException("Muse reply too large");
        return true;
    }

    private static String string(JSONObject object, String key) {
        return object.opt(key) instanceof String value ? value : "";
    }

    private static String finalText(JSONObject data, String id) {
        if (data.opt("display_text") instanceof String text && !text.isBlank()) return text;
        if (data.opt("transcript") instanceof String text && !text.isBlank()) return text;
        var transcript = data.optJSONObject("transcript");
        var messages = transcript == null ? null : transcript.optJSONArray("messages");
        if (messages != null) for (int i = messages.length() - 1; i >= 0; i--) {
            var message = messages.optJSONObject(i);
            if (message == null || !"assistant".equals(string(message, "role")) || !id.equals(string(message, "id"))) continue;
            var content = message.optJSONArray("content");
            var parts = new ArrayList<String>();
            if (content != null) for (int j = 0; j < content.length(); j++) {
                var part = content.optJSONObject(j);
                if (part != null && "text".equals(string(part, "type"))) parts.add(string(part, "text"));
            }
            String text = String.join("\n", parts);
            if (!text.isBlank()) return text;
            break;
        }
        return string(data, "content");
    }

    private static MuseWire.Frame receive(MuseSocket socket, MuseNoise noise, MuseWire wire) throws Exception {
        byte[] bytes = socket.receive();
        return bytes == null ? null : wire.decode(noise.decrypt(bytes));
    }

    private static long now() { return System.nanoTime() / 1_000_000; }
}
