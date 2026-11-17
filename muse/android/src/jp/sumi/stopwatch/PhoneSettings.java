package jp.sumi.stopwatch;

import android.content.Context;
import android.content.SharedPreferences;

final class PhoneSettings {
    private final SharedPreferences preferences;
    private final LocalSecrets secrets;

    PhoneSettings(Context context) {
        preferences = context.getSharedPreferences("voice", Context.MODE_PRIVATE);
        secrets = new LocalSecrets(context);
    }

    String asr() { return preferences.getString("asr", "openai"); }
    String tts() { return preferences.getString("tts", "irodori"); }
    void provider(String kind, String id) { preferences.edit().putString(kind, id).apply(); }
    String openAiKey() { return secrets.read("openai"); }
    boolean hasCredential(String name) { return secrets.has(name); }
    boolean saveOpenAiKey(String value) { return secrets.save("openai", value); }
    synchronized String museCredential() { return secrets.read("muse"); }
    synchronized boolean replaceMuseCredential(String expected, String replacement) {
        return expected.equals(museCredential()) && saveMuseCredential(replacement);
    }
    synchronized boolean saveMuseCredential(String value) { return secrets.save("muse", value); }
    String museRotation(String watch) { return secrets.read("muse_rotation_" + watch); }
    boolean saveMuseRotation(String watch, String value) { return secrets.save("muse_rotation_" + watch, value); }
    String musePair(String watch) { return secrets.read("muse_pair_" + watch); }
    boolean saveMusePair(String watch, String value) { return secrets.save("muse_pair_" + watch, value); }

    ChatCatalog chatCatalog() {
        String value = preferences.getString("muse_chat_catalog", "");
        if (value.isEmpty()) return ChatCatalog.DEFAULT;
        try { return ChatCatalog.read(value); }
        catch (org.json.JSONException e) { return ChatCatalog.DEFAULT; }
    }

    boolean saveChatCatalog(ChatCatalog catalog) {
        try { return preferences.edit().putString("muse_chat_catalog", catalog.encode()).commit(); }
        catch (org.json.JSONException e) { return false; }
    }
    boolean echo() { return preferences.getBoolean("echo", false); }
    void echo(boolean enabled) { preferences.edit().putBoolean("echo", enabled).apply(); }

    String deviceAddress() { return preferences.getString("stopwatch_address", ""); }
    String deviceName() { return preferences.getString("stopwatch_name", "StopWatch"); }
    boolean autoConnect() { return preferences.getBoolean("auto_connect", true); }
    void autoConnect(boolean enabled) { preferences.edit().putBoolean("auto_connect", enabled).apply(); }
    void saveDevice(String address, String name) {
        preferences.edit().putString("stopwatch_address", address).putString("stopwatch_name", name).apply();
    }
    boolean logText() { return preferences.getBoolean("log_text", true); }
    void logText(boolean enabled) { preferences.edit().putBoolean("log_text", enabled).apply(); }
}
