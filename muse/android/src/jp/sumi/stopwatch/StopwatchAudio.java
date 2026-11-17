package jp.sumi.stopwatch;

import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import java.util.Arrays;
import java.util.concurrent.ThreadLocalRandom;
import java.util.function.Consumer;

/** One turn at a time. All calls run on the same handler thread as the GATT queue. */
final class StopwatchAudio {
    record Recording(byte[] pcm, String session, boolean wearable) {}
    interface Reply {
        void play(byte[] pcm);
        boolean streamAvailable();
        default boolean prefillAvailable() { return false; }
        default void allowPlayback() {}
        void generating();
        void segment(byte[] pcm, boolean last, Runnable sent);
        void finishStream();
        void fail(String message);
        void caption(String text);
    }
    interface Speech {
        void start(Recording note, Reply reply);
        void cancel();
    }
    interface Link {
        void write(AudioPacket packet, boolean acknowledged, Runnable next);
        void offerPush(AudioPacket packet, Runnable rejected);
        void readStatus(Consumer<AudioPacket> next);
        void after(Runnable next, long millis);
    }

    private final Link link;
    private final Speech speech;
    private final Consumer<String> status;
    private final Consumer<Boolean> active;
    private final ConversationLog log = ConversationLog.INSTANCE;
    private int turn, received, offset, segmentStart;
    private long playbackStart;
    private String session;
    private final byte[] scope = new byte[ChatCatalog.ID_BYTES];
    private int scopeReceived;
    private boolean wearable;
    private byte[] recording;
    private byte[] reply;
    private int lastAck;
    private int stalls;
    private int mtu = 23;
    private boolean streamAvailable, pushAvailable, prefillAvailable, pushing, streamOpen, lastSegment;
    private boolean streamingNote, prefillReply, replyBegun, playbackAllowed, playbackSignalled;
    private Runnable segmentSent;
    private Consumer<Reply> pushReady;

    StopwatchAudio(Link link, Speech speech, Consumer<String> status, Consumer<Boolean> active) {
        this.link = link;
        this.speech = speech;
        this.status = status;
        this.active = active;
    }

    void mtu(int value) { mtu = value; }

    void features(int flags) {
        streamAvailable = (flags & AudioPacket.FEATURE_STREAM) != 0;
        pushAvailable = (flags & AudioPacket.FEATURE_PUSH) != 0;
        prefillAvailable = (flags & AudioPacket.FEATURE_REPLY_PREFILL) != 0;
    }

    boolean supportsPush() { return pushAvailable; }
    boolean busy() { return turn != 0; }
    boolean recordingTurn() { return busy() && !pushing; }

    void reset() {
        boolean wasActive = turn != 0;
        turn = 0;
        recording = reply = null;
        streamOpen = pushing = false;
        streamingNote = prefillReply = replyBegun = playbackAllowed = playbackSignalled = false;
        pushReady = null;
        segmentSent = null;
        speech.cancel();
        if (wasActive) active.accept(false);
    }

    void push(ChatCatalog.Watch selection, Consumer<Reply> ready) {
        if (busy() || !pushAvailable) return;
        int candidate = ThreadLocalRandom.current().nextInt();
        int generation = candidate == 0 ? 1 : candidate;
        turn = generation;
        pushing = true;
        pushReady = ready;
        active.accept(true);
        status.accept("Museの返信をC152へ送信中…");
        Runnable refused = () -> {
            if (turn != generation || pushReady == null) return;
            log.event(ConversationLog.BLE, "C152が外部返信の再生を開始できませんでした");
            status.accept("C152が返信の再生を開始できませんでした");
            reset();
        };
        link.offerPush(new AudioPacket(AudioPacket.PUSH_BEGIN, turn, selection.hash(),
                new byte[]{(byte) selection.selected()}), refused);
        link.after(refused, 6_000);
    }

    void onNotification(byte[] bytes) {
        AudioPacket packet;
        try { packet = AudioPacket.decode(bytes); }
        catch (IllegalArgumentException ignored) { return; }
        if (packet.type() == AudioPacket.NOTE_BEGIN) {
            begin(packet);
            return;
        }
        if (turn == 0 || packet.turn() != turn) return;
        switch (packet.type()) {
            case AudioPacket.PUSH_READY -> {
                if (pushing && pushReady != null && packet.offset() == 0 && packet.data().length == 0) {
                    Consumer<Reply> ready = pushReady;
                    pushReady = null;
                    ready.accept(output());
                }
            }
            case AudioPacket.PUSH_DONE -> {
                if (pushing && packet.offset() == 0 && packet.data().length == 0) {
                    log.event(ConversationLog.BLE, "C152で外部返信の再生完了");
                    status.accept("C152でMuseの返信を再生しました");
                    reset();
                }
            }
            case AudioPacket.NOTE_PCM -> receive(packet);
            case AudioPacket.NOTE_SCOPE -> receiveScope(packet);
            case AudioPacket.NOTE_END -> finish(packet);
            case AudioPacket.CANCEL -> {
                log.event(ConversationLog.BLE, "C152が会話を中断");
                reset();
                status.accept("会話を中断しました");
            }
            default -> fail("音声データの形式が違います");
        }
    }

    private void begin(AudioPacket packet) {
        int length = packet.offset();
        boolean streaming = length == AudioPacket.STREAM_LENGTH;
        if (packet.turn() == 0 ||
                (!streaming && (length <= 0 || length > AudioPacket.MAX_NOTE || (length & 1) != 0)) ||
                packet.data().length != 2 || packet.data()[0] < 0 || packet.data()[0] > 2 ||
                packet.data()[1] < 0 || packet.data()[1] > 1) return;
        reset();
        turn = packet.turn();
        active.accept(true);
        streamingNote = streaming;
        recording = new byte[streaming ? AudioPacket.MAX_NOTE : length];
        session = switch (packet.data()[0]) {
            case 0 -> "";
            case 1 -> ChatCatalog.LEGACY_ID;
            default -> null;
        };
        scopeReceived = 0;
        wearable = packet.data()[1] == 1;
        received = lastAck = 0;
        status.accept(streaming ? "C152で録音・転送中" : "C152から録音を受信中");
        log.event(ConversationLog.BLE, "C152 → スマホ · 録音受信開始 · " + (streaming ? "録音中の転送" : length + " B"));
    }

    private void receive(AudioPacket packet) {
        byte[] data = packet.data();
        if (recording == null || packet.offset() != received || data.length == 0 ||
                (data.length & 1) != 0 || data.length > recording.length - received) {
            fail("録音データが途中で欠けました");
            return;
        }
        System.arraycopy(data, 0, recording, received, data.length);
        received += data.length;
        if (received == recording.length || received - lastAck >= 2048) {
            lastAck = received;
            link.write(new AudioPacket(AudioPacket.NOTE_ACK, turn, received), true, null);
        }
    }

    private void receiveScope(AudioPacket packet) {
        byte[] data = packet.data();
        if (recording == null || session != null || received != 0 || packet.offset() != scopeReceived ||
                data.length == 0 || data.length > scope.length - scopeReceived) {
            fail("C152の会話先を受け取れませんでした");
            return;
        }
        System.arraycopy(data, 0, scope, scopeReceived, data.length);
        scopeReceived += data.length;
        if (scopeReceived == scope.length) {
            String supplied = new String(scope, java.nio.charset.StandardCharsets.US_ASCII);
            if (ChatCatalog.isSideId(supplied)) session = supplied;
            else fail("C152の会話先が不正です");
        }
    }

    private void finish(AudioPacket packet) {
        if (recording == null || packet.data().length != 0 || received == 0 || packet.offset() != received ||
                (!streamingNote && received != recording.length)) {
            fail("録音の終端が一致しません");
            return;
        }
        if (session == null) { fail("C152の会話先が届きませんでした"); return; }
        byte[] pcm = received == recording.length ? recording : Arrays.copyOf(recording, received);
        Recording note = new Recording(pcm, session, wearable);
        recording = null;
        status.accept("C152から録音を受信しました");
        log.event(ConversationLog.BLE, "C152 → スマホ · 録音受信完了 · " + received + " B");
        speech.start(note, output());
    }

    private Reply output() {
        int generation = turn;
        return new Reply() {
            @Override public void play(byte[] pcm) {
                if (turn == generation) sendReply(pcm);
            }
            @Override public boolean streamAvailable() { return streamAvailable; }
            @Override public boolean prefillAvailable() { return streamAvailable && prefillAvailable; }
            @Override public void allowPlayback() {
                if (turn != generation) return;
                playbackAllowed = true;
                signalPlayback();
            }
            @Override public void generating() {
                if (turn == generation && streamAvailable)
                    link.write(new AudioPacket(AudioPacket.GENERATING, turn, 0), true, null);
            }
            @Override public void segment(byte[] pcm, boolean last, Runnable sent) {
                if (turn == generation) sendSegment(pcm, last, sent);
            }
            @Override public void finishStream() {
                if (turn == generation && streamOpen && reply == null) endReply(generation);
            }
            @Override public void fail(String message) {
                if (turn == generation) StopwatchAudio.this.fail(message);
            }
            @Override public void caption(String text) {
                if (turn != generation) return;
                byte[] utf8 = text.getBytes(java.nio.charset.StandardCharsets.UTF_8);
                int maximum = Math.min(235, AudioPacket.pcmCapacity(mtu));
                int length = Math.min(utf8.length, maximum);
                while (length > 0 && length < utf8.length && (utf8[length] & 0xc0) == 0x80) --length;
                if (length > 0) link.write(new AudioPacket(AudioPacket.CAPTION, turn, 0,
                        Arrays.copyOf(utf8, length)), true, null);
            }
        };
    }

    private void sendReply(byte[] pcm) {
        startReply(pcm, pcm.length, true, null);
    }

    private void sendSegment(byte[] pcm, boolean last, Runnable sent) {
        if (!streamOpen) {
            streamOpen = true;
            startReply(pcm, AudioPacket.STREAM_LENGTH, last, sent);
        } else if (setSegment(pcm, last, sent)) {
            check(turn);
        }
    }

    private void startReply(byte[] pcm, int length, boolean last, Runnable sent) {
        offset = 0;
        if (!setSegment(pcm, last, sent)) return;
        playbackStart = System.nanoTime();
        int generation = turn;
        prefillReply = length == AudioPacket.STREAM_LENGTH && prefillAvailable;
        int mode = prefillReply ? AudioPacket.REPLY_PREFILL_LENGTH : length;
        link.write(new AudioPacket(AudioPacket.REPLY_BEGIN, turn, mode), true, () -> {
            if (generation != turn) return;
            replyBegun = true;
            signalPlayback();
            check(generation);
        });
    }

    private void signalPlayback() {
        if (!prefillReply || !replyBegun || !playbackAllowed || playbackSignalled) return;
        playbackSignalled = true;
        link.write(new AudioPacket(AudioPacket.REPLY_PLAY, turn, 0), true, null);
    }

    private boolean setSegment(byte[] pcm, boolean last, Runnable sent) {
        if (reply != null || (!last && sent == null) || pcm.length == 0 || pcm.length > AudioPacket.MAX_REPLY - offset ||
                (pcm.length & 1) != 0 || AudioPacket.pcmCapacity(mtu) == 0) {
            fail("返答音声の長さが不正です");
            return false;
        }
        reply = pcm;
        segmentStart = offset;
        stalls = 0;
        lastSegment = last;
        segmentSent = sent;
        status.accept(streamOpen ? "C152へ音声を転送中…" : "C152で再生を準備中…");
        return true;
    }

    private void check(int generation) {
        if (generation != turn || reply == null) return;
        link.readStatus(packet -> {
            if (generation != turn || reply == null) return;
            if (packet.type() != AudioPacket.REPLY_STATUS || packet.turn() != generation || packet.data().length != 4) {
                fail("C152側で会話が終了しました");
                return;
            }
            int accepted = packet.offset();
            int free = ByteBuffer.wrap(packet.data()).order(ByteOrder.LITTLE_ENDIAN).getInt();
            if (accepted < segmentStart || accepted > offset || (accepted & 1) != 0 || free < 0 || free > 65536) {
                fail("C152の受信状態が不正です");
                return;
            }
            stalls = accepted < offset || free == 0 ? stalls + 1 : 0;
            if (stalls > 100) { fail("音声を送信できません"); return; }
            offset = accepted;
            if (offset == segmentStart + reply.length) {
                if (lastSegment) {
                    endReply(generation);
                } else {
                    Runnable next = segmentSent;
                    reply = null;
                    segmentSent = null;
                    next.run();
                }
            } else if (free >= 2) {
                burst(generation, offset + Math.min(free & ~1, 4096));
            } else {
                link.after(() -> check(generation), 20);
            }
        });
    }

    private void endReply(int generation) {
        link.write(new AudioPacket(AudioPacket.REPLY_END, generation, offset), true, () -> {
            if (generation != turn) return;
            status.accept("C152へ音声を転送しました（" + elapsedMs(playbackStart) + " ms）");
            log.event(ConversationLog.BLE, "スマホ → C152 · 音声転送完了 · " + offset + " B · " +
                    elapsedMs(playbackStart) + " ms");
            if (!pushing) reset();
            else link.after(() -> {
                if (turn != generation) return;
                log.event(ConversationLog.BLE, "C152の外部返信の再生完了を確認できませんでした");
                reset();
            }, 6_000);
        });
    }

    private void burst(int generation, int end) {
        if (generation != turn || reply == null) return;
        int length = Math.min(AudioPacket.pcmCapacity(mtu), Math.min(end, segmentStart + reply.length) - offset);
        if (length == 0) { check(generation); return; }
        byte[] fragment = Arrays.copyOfRange(reply, offset - segmentStart, offset - segmentStart + length);
        AudioPacket packet = new AudioPacket(AudioPacket.REPLY_PCM, turn, offset, fragment);
        offset += length;
        link.write(packet, false, () -> burst(generation, end));
    }

    private void fail(String message) {
        log.event(ConversationLog.BLE, "会話終了 · " + message);
        if (turn != 0) link.write(new AudioPacket(AudioPacket.ERROR, turn, 0), true, null);
        reset();
        status.accept(message);
    }

    private static long elapsedMs(long started) { return (System.nanoTime() - started) / 1_000_000; }
}
