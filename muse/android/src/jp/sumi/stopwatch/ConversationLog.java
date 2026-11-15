package jp.sumi.stopwatch;

import android.os.Handler;
import android.os.Looper;
import java.util.ArrayDeque;
import java.util.List;

final class ConversationLog {
    static final int LIMIT = 300;
    static final String BLE = "BLE", OPENAI = "OpenAI", ANDROID = "Android", MUSE = "Muse", IRODORI = "Irodori";
    static final ConversationLog INSTANCE = new ConversationLog();

    record Entry(long time, String source, String message, boolean body) {}

    private final Handler main = new Handler(Looper.getMainLooper());
    private final ArrayDeque<Entry> entries = new ArrayDeque<>();
    private Runnable listener;
    private boolean includeText = true;
    private final Runnable changed = () -> { if (listener != null) listener.run(); };

    void listen(Runnable listener) { this.listener = listener; }

    synchronized void includeText(boolean enabled) {
        includeText = enabled;
        if (!enabled) entries.removeIf(Entry::body);
        invalidate();
    }

    void event(String source, String message) { add(source, message, false); }
    void body(String source, String stage, String text) { add(source, stage + "\n" + text, true); }

    private synchronized void add(String source, String message, boolean body) {
        if (body && !includeText) return;
        int end = Math.min(8000, message.length());
        if (end > 0 && Character.isHighSurrogate(message.charAt(end - 1))) end--;
        if (entries.size() == LIMIT) entries.removeFirst();
        entries.addLast(new Entry(System.currentTimeMillis(), source,
                end < message.length() ? message.substring(0, end) + "…" : message, body));
        invalidate();
    }

    synchronized List<Entry> snapshot() { return List.copyOf(entries); }

    synchronized void clear() {
        entries.clear();
        invalidate();
    }

    private void invalidate() {
        main.removeCallbacks(changed);
        main.post(changed);
    }
}
