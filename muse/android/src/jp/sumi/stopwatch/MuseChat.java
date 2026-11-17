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
import java.util.concurrent.CancellationException;
import java.util.concurrent.ExecutorService;
import java.util.function.BiConsumer;
import java.util.function.Consumer;
import java.util.function.Supplier;

final class MuseChat implements SpeechBackend.Chat {
    private static final ConversationLog LOG = ConversationLog.INSTANCE;
    private static final long SETTLE_MS = 3000;
    private static final long REPLY_TIMEOUT_MS = 60_000, CATALOG_TIMEOUT_MS = 120_000;
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

    SpeechBackend.Task verifyAccess(String access, SpeechBackend.Result<String> result) {
        return NetworkTask.start(workers, result, "Museの認証を確認できませんでした", request -> {
            vm(request, new Settings(access, ""));
            return access;
        });
    }

    record Rotation(int status, String access, String refresh) {
        boolean succeeded() { return status == 200; }
    }

    static Rotation rotate(NetworkTask request, MusePair pair) throws Exception {
        String credential = pair.refresh();
        String suffix = credential.substring(credential.lastIndexOf(':') + 1);
        var http = request.open(URI.create("https://api.muse.ai/device_token/refresh"));
        http.setRequestMethod("POST");
        http.setRequestProperty("Authorization", "Bearer hatch_refresh:" + suffix);
        http.setRequestProperty("Content-Type", "application/json");
        http.setRequestProperty("X-API-Version", "1.0.0");
        byte[] body = new JSONObject().put("device_id", pair.device()).toString().getBytes(StandardCharsets.UTF_8);
        http.setDoOutput(true);
        // Fixed-length streaming prevents the HTTP client from replaying a POST on authentication/redirect.
        http.setFixedLengthStreamingMode(body.length);
        try (var out = http.getOutputStream()) { out.write(body); }
        int status = http.getResponseCode();
        LOG.event(ConversationLog.MUSE, "POST /device_token/refresh · HTTP " + status);
        if (status != 200) return new Rotation(status, "", "");
        try (var in = http.getInputStream()) {
            JSONObject value = new JSONObject(new String(OpenAiAsr.readBounded(in, 64 * 1024), StandardCharsets.UTF_8));
            JSONObject payload = value.optJSONObject("payload");
            if (payload != null) value = payload;
            String access = value.getString("access_token");
            String refresh = value.getString("refresh_token");
            new MusePair(0, false, pair.device(), access, refresh);
            return new Rotation(status, access, refresh);
        }
    }

    static boolean accessRejected(String error) {
        return error.startsWith("Museが端末アクセストークンを拒否しました");
    }

    private SpeechBackend.Task run(SpeechBackend.ChatRequest call, SpeechBackend.Result<String> reply) {
        String auth = credential.get();
        var receive = call == null ? reply : SpeechBackend.<String>result(reply::success,
                error -> call.transcript().thenAccept(text -> reply.failure(error)));
        return NetworkTask.start(workers, receive, "Museとの通信に失敗しました", request -> {
            long started = now();
            Vm vm = loadVm(request, auth);
            if (call != null && !call.catalog()) Log.i("SumiLatency", "Muse VM lookup in " + (now() - started) + " ms");
            return chat(request, vm, call);
        });
    }

    private static Vm loadVm(NetworkTask request, String auth) throws NetworkTask.Failure {
        if (auth.isBlank()) throw new NetworkTask.Failure("会話設定のMuseからC152の認証を取り込んでください");
        try {
            JSONObject settings = new JSONObject(auth);
            return vm(request, new Settings(settings.getString("token"), settings.optString("vm")));
        } catch (Exception e) {
            throw stageError("アカウント確認", e);
        }
    }

    SpeechBackend.Task watch(String session, Runnable ready, BiConsumer<String, String> reply,
                             Consumer<String> failed) {
        String auth = credential.get();
        return NetworkTask.start(workers, SpeechBackend.<Void>result(unused -> {}, failed),
                "Museの返信待受が切断されました", request -> {
                    Vm vm = loadVm(request, auth);
                    try (var socket = new MuseSocket(vm.id, vm.token, request)) {
                        var noise = new MuseNoise();
                        var wire = new MuseWire();
                        handshake(socket, noise);
                        MuseWire.Frame frame = subscribe(socket, noise, wire, scope(session));
                        ready.run();
                        var lines = new MuseWire.EventLines();
                        var live = new LiveReplies(session, reply);
                        long nextPing = now() + 25_000;
                        while (true) {
                            if (frame != null && frame.stream() == 1) {
                                if (frame.end()) throw new IOException("Muse subscription closed");
                                for (byte value : frame.body()) {
                                    String line = lines.append(value);
                                    if (line == null || line.isBlank()) continue;
                                    try { live.feed(new JSONObject(line)); }
                                    catch (JSONException malformed) { continue; }
                                }
                            }
                            if (now() >= nextPing) {
                                socket.ping();
                                nextPing = now() + 25_000;
                            }
                            frame = receive(socket, noise, wire);
                        }
                    }
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
        String text = call == null ? null : call.transcript().getNow(null);
        if (text != null) checkTextLength(text);
        String stage = "VM接続";
        long connectionStarted = now();
        try (MuseSocket socket = new MuseSocket(vm.id, vm.token, request)) {
            stage = "暗号化接続";
            MuseNoise noise = new MuseNoise();
            MuseWire wire = new MuseWire();
            handshake(socket, noise);
            LOG.event(ConversationLog.MUSE, "VM · 暗号化接続完了");
            if (call == null) return "Muse VMに接続できました";
            long connected = now();
            if (!call.catalog()) Log.i("SumiLatency", "Muse TLS and Noise in " + (connected - connectionStarted) + " ms");
            if (text == null) {
                text = call.transcript().get();
                checkTextLength(text);
            }

            stage = "会話先の購読";
            long subscribing = now();
            String session = call.session();
            JSONObject scope = scope(session);
            MuseWire.Frame first = subscribe(socket, noise, wire, scope);
            if (!call.catalog()) Log.i("SumiLatency", "Muse subscription in " + (now() - subscribing) + " ms");
            stage = "メッセージ送信";
            JSONObject post = new JSONObject(scope.toString()).put("message", text).put("output_modality", "text");
            if (call.wearable()) post.put("device", "mcu-wearable");
            socket.send(noise.encrypt(wire.request(2, "/chat/stream", post.toString(), false)));
            if (call.catalog()) LOG.event(ConversationLog.MUSE, "チャット一覧を取得中 · " +
                    (session.isEmpty() ? "メインチャット" : "選択中のサイドチャット"));
            else LOG.body(ConversationLog.MUSE, "送信 · POST /chat/stream", text);
            stage = "返答の受信";
            return reply(socket, noise, wire, first, call);
        } catch (InterruptedException | CancellationException e) {
            throw e;
        } catch (Exception e) {
            throw stageError(stage, e);
        }
    }

    private static void checkTextLength(String text) throws NetworkTask.Failure {
        if (text.getBytes(StandardCharsets.UTF_8).length > 8192)
            throw new NetworkTask.Failure("文字起こしが長すぎます");
    }

    private static void handshake(MuseSocket socket, MuseNoise noise) throws Exception {
        socket.send(noise.start());
        long begin = now();
        byte[] response;
        while ((response = socket.receive()) == null && now() - begin < 12_000) {}
        if (response == null) throw new IOException("Muse handshake timed out");
        socket.send(noise.finish(response));
    }

    private static JSONObject scope(String session) throws Exception {
        if (session.isEmpty()) return new JSONObject();
        if (!ChatCatalog.isSideId(session)) throw new NetworkTask.Failure("Museの会話先が不正です");
        return new JSONObject().put("session_id", session);
    }

    private static MuseWire.Frame subscribe(MuseSocket socket, MuseNoise noise, MuseWire wire,
                                             JSONObject scope) throws Exception {
        socket.send(noise.encrypt(wire.request(1, "/chat/subscribe", scope.toString(), true)));
        long until = now() + 18_000;
        while (now() < until) {
            MuseWire.Frame frame = receive(socket, noise, wire);
            if (frame == null || frame.stream() != 1 || (frame.status() == 0 && !frame.end())) continue;
            if (frame.status() >= 200 && frame.status() < 300 && !frame.end()) return frame;
            break;
        }
        throw new NetworkTask.Failure("Museの会話先を購読できません");
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

    static final class LiveReplies {
        private static final long WINDOW_MS = 180_000;
        private final String session;
        private final BiConsumer<String, String> reply;
        private final LinkedHashMap<String, Long> users = new LinkedHashMap<>();
        private final LinkedHashMap<String, Message> messages = new LinkedHashMap<>();
        private String mainSession = "";
        private long lastUserSequence, lastUserAt;

        LiveReplies(String session, BiConsumer<String, String> reply) {
            this.session = session;
            this.reply = reply;
        }

        void feed(JSONObject record) {
            if (!"event".equals(record.optString("type"))) return;
            JSONObject data = record.optJSONObject("payload");
            if (data == null) return;
            String actual = eventSession(record, data);
            if (session.isEmpty()) {
                if (data.optBoolean("is_thread") || record.optBoolean("is_thread") ||
                        (!mainSession.isEmpty() && !actual.isEmpty() && !mainSession.equals(actual))) return;
            } else if (!session.equals(actual)) return;
            String type = record.optString("event");
            String id = eventId(record, data);
            if (id.isBlank()) return;
            long sequence = record.optLong("seq");
            long time = now();
            if ("message.user".equals(type)) {
                if (users.containsKey(id) || (sequence > 0 && sequence <= lastUserSequence)) return;
                if (session.isEmpty() && mainSession.isEmpty()) mainSession = actual;
                if (users.size() >= 16) users.remove(users.keySet().iterator().next());
                users.put(id, time);
                lastUserSequence = sequence;
                lastUserAt = time;
                return;
            }
            boolean append = "delta.text_append".equals(type);
            boolean full = "message.assistant".equals(type);
            boolean done = "delta.message_done".equals(type) || full;
            if ((!append && !done) || "user".equals(string(data, "role"))) return;
            Message message = messages.get(id);
            if (message == null) {
                String parent = string(data, "reply_to_message_id");
                if (parent.isEmpty()) parent = string(data, "parent_message_id");
                Long submitted = users.get(parent);
                boolean linked = submitted != null && time - submitted <= WINDOW_MS;
                boolean parentless = (parent.isEmpty() || id.equals(parent)) && !users.isEmpty() &&
                        time - lastUserAt <= WINDOW_MS &&
                        (sequence == 0 || lastUserSequence == 0 || sequence > lastUserSequence);
                if (!linked && !parentless) return;
                if (messages.size() >= 8) messages.remove(messages.keySet().iterator().next());
                message = new Message();
                messages.put(id, message);
            }
            if (message.done) return;
            if (append) {
                String delta = string(data, "text");
                if (delta.length() > TEXT_BYTES_MAX - message.text.length()) {
                    message.text.setLength(0);
                    message.done = true;
                    return;
                }
                message.text.append(delta);
            }
            if (!done || (full && !data.optBoolean("display_text_ready", true))) return;
            String text = finalText(data, id);
            if (text.isBlank()) text = message.text.toString();
            text = MuseReplyText.withoutContextStatus(text).strip();
            if (text.isBlank()) return;
            message.done = true;
            message.text.setLength(0);
            if (text.length() <= TEXT_BYTES_MAX) reply.accept(id, text);
        }
    }

    private static final class ReplyBinding {
        final HashSet<String> userIds = new HashSet<>();
        final String catalogId;
        final long postedAt = now();
        String mainSession;
        String catalogReply;
        boolean sawCatalogText;
        long lastUserSequence;
        boolean lastUserIsOurs;
        boolean completed;
        boolean sawDelta;
        boolean busy;
        boolean taskFinished;

        ReplyBinding(String catalogId) { this.catalogId = catalogId; }

        boolean catalog() { return !catalogId.isEmpty(); }

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

    private static String reply(MuseSocket socket, MuseNoise noise, MuseWire wire, MuseWire.Frame frame,
                                SpeechBackend.ChatRequest call) throws Exception {
        ByteArrayOutputStream ack = new ByteArrayOutputStream();
        MuseWire.EventLines lines = new MuseWire.EventLines();
        ArrayList<JSONObject> pending = new ArrayList<>();
        String session = call.session();
        ReplyBinding binding = new ReplyBinding(call.catalogId());
        LinkedHashMap<String, Message> messages = new LinkedHashMap<>();
        String reported = "";
        boolean accepted = false;
        int pendingBytes = 0;
        long last = now(), after = last + (binding.catalog() ? CATALOG_TIMEOUT_MS : REPLY_TIMEOUT_MS);
        while (now() < after) {
            if (accepted && !binding.catalog() && !binding.busy && !messages.isEmpty() &&
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
                    if (session.isEmpty()) binding.mainSession = string(object, "session_id");
                    for (String key : new String[]{"message_id", "reply_to_message_id"}) {
                        String value = string(object, key);
                        if (!value.isBlank()) binding.userIds.add(value);
                    }
                    if (binding.userIds.isEmpty()) throw new IOException("No Muse message id");
                    accepted = true;
                    for (JSONObject event : pending) {
                        event(event, session, binding, messages);
                        if (binding.catalogReply != null) break;
                    }
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
                        if (binding.catalogReply != null) break;
                    } else if (relevant(event, session, binding)) {
                        pendingBytes += json.getBytes(StandardCharsets.UTF_8).length;
                        if (pending.size() >= 128 || pendingBytes > PENDING_BYTES_MAX)
                            throw new IOException("Too many unacknowledged Muse events");
                        pending.add(event);
                    }
                }
            }
            if (binding.catalogReply != null) {
                LOG.event(ConversationLog.MUSE, "チャット一覧 · Museの応答を受信");
                return binding.catalogReply;
            }
            if (accepted && binding.completed && call.completedText() != null) {
                binding.completed = false;
                String complete = completedPrefix(messages);
                if (!complete.equals(reported)) {
                    call.completedText().accept(complete);
                    reported = complete;
                }
            }
            frame = null;
        }
        if (binding.catalog()) {
            LOG.event(ConversationLog.MUSE, binding.sawCatalogText ?
                    "チャット一覧 · 更新IDが一致するJSONがありません" : "チャット一覧 · 返答本文の受信なし");
            if (binding.sawCatalogText) throw new NetworkTask.Failure("Museの一覧返答を照合できませんでした");
        } else if (accepted && !messages.isEmpty() && messages.values().stream().allMatch(m -> m.done)) {
            return assemble(messages);
        }
        throw new NetworkTask.Failure("Museの返答がタイムアウトしました");
    }

    private static String completedPrefix(LinkedHashMap<String, Message> messages) {
        var parts = new ArrayList<String>();
        for (Message message : messages.values()) {
            if (!message.done) break;
            String body = MuseReplyText.withoutContextStatus(message.text.toString().strip());
            if (!body.isEmpty()) parts.add(body);
        }
        return String.join("\n", parts);
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

    private static boolean relevant(JSONObject record, String session, ReplyBinding binding) {
        if (!"event".equals(record.optString("type"))) return false;
        JSONObject data = record.optJSONObject("payload");
        if (data == null) return false;
        String actual = eventSession(record, data);
        if (session.isEmpty() && binding.catalog()) {
            if (data.optBoolean("is_thread") || record.optBoolean("is_thread")) return false;
            // The main chat can carry the session ID supplied in the acknowledgement.
            if (!actual.isEmpty() && binding.mainSession != null && !binding.mainSession.isEmpty() &&
                    !binding.mainSession.equals(actual)) return false;
        } else if (!session.equals(actual)) return false;
        return switch (record.optString("event")) {
            case "agent.status", "task.status", "message.user", "delta.text_append", "message.assistant",
                    "delta.message_done", "delta.message_start" -> true;
            default -> false;
        };
    }

    private static boolean event(JSONObject record, String session, ReplyBinding binding,
                                 LinkedHashMap<String, Message> messages) throws Exception {
        if (!relevant(record, session, binding)) return false;
        JSONObject data = record.optJSONObject("payload");
        String type = record.optString("event");
        long sequence = record.optLong("seq");
        String id = eventId(record, data);
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
            boolean unnamed = parent.isEmpty() || id.equals(parent);
            if (!binding.catalog() && !linked &&
                    !(unnamed && !session.isEmpty() && binding.followsOurMessage(sequence))) return false;
            if (messages.size() >= 8) throw new IOException("Too many Muse replies");
            message = new Message();
            messages.put(id, message);
        }
        boolean finished = done && (!full || data.optBoolean("display_text_ready", true));
        if (message.done && !(binding.catalog() && finished)) return false;
        if (append) {
            String chunk = string(data, "text");
            message.text.append(chunk);
            if (!binding.catalog() && !binding.sawDelta && !chunk.isBlank()) {
                binding.sawDelta = true;
                Log.i("SumiLatency", "First Muse text delta after send in " + (now() - binding.postedAt) + " ms");
            }
        }
        if (finished) {
            String text = finalText(data, id);
            if (!text.isBlank()) {
                message.text.setLength(0);
                message.text.append(text);
            }
            message.done = true;
            binding.completed = true;
        }
        if (message.text.length() > TEXT_BYTES_MAX) throw new IOException("Muse reply too large");
        if (binding.catalog()) {
            binding.sawCatalogText |= !message.text.isEmpty();
            if (finished) {
                String body = MuseReplyText.withoutContextStatus(message.text.toString());
                if (ChatCatalog.matchesRequest(body, binding.catalogId)) binding.catalogReply = body;
            }
        }
        return true;
    }

    private static String string(JSONObject object, String key) {
        return object.opt(key) instanceof String value ? value : "";
    }

    private static String eventSession(JSONObject record, JSONObject data) {
        String session = string(data, "session_id");
        return session.isEmpty() ? string(record, "session_id") : session;
    }

    private static String eventId(JSONObject record, JSONObject data) {
        String id = string(data, "message_id");
        if (id.isEmpty()) id = string(record, "message_id");
        return id.isEmpty() ? string(data, "id") : id;
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
