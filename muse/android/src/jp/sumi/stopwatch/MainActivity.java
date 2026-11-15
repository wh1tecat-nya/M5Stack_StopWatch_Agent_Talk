package jp.sumi.stopwatch;

import android.Manifest;
import android.animation.ValueAnimator;
import android.app.Activity;
import android.app.AlertDialog;
import android.bluetooth.BluetoothAdapter;
import android.bluetooth.BluetoothDevice;
import android.bluetooth.BluetoothManager;
import android.content.ClipData;
import android.content.ClipboardManager;
import android.content.ComponentName;
import android.content.Context;
import android.content.Intent;
import android.content.ServiceConnection;
import android.content.pm.PackageManager;
import android.content.res.ColorStateList;
import android.graphics.Color;
import android.graphics.Typeface;
import android.graphics.drawable.GradientDrawable;
import android.net.Uri;
import android.os.Bundle;
import android.os.IBinder;
import android.text.InputType;
import android.view.Gravity;
import android.view.View;
import android.view.ViewGroup;
import android.view.WindowInsets;
import android.view.animation.DecelerateInterpolator;
import android.widget.BaseAdapter;
import android.widget.Button;
import android.widget.EditText;
import android.widget.FrameLayout;
import android.widget.LinearLayout;
import android.widget.ListView;
import android.widget.RadioButton;
import android.widget.RadioGroup;
import android.widget.ScrollView;
import android.widget.SeekBar;
import android.widget.Switch;
import android.widget.TextView;
import android.widget.Toast;
import org.json.JSONObject;

import java.io.File;
import java.text.SimpleDateFormat;
import java.util.ArrayList;
import java.util.Collections;
import java.util.Date;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.function.IntConsumer;

public final class MainActivity extends Activity {
    private static final int INK = 0xff172822, GREEN = 0xff20644f, MUTED = 0xff506157;
    private static final int PAPER = 0xfff4f6f2, CARD = Color.WHITE, PALE = 0xffdef0e4, LINE = 0xffd6e2d8;
    private static final int BLE = 1, ENABLE = 2, MIC = 3, MODEL = 4, VOICE = 5, MUSE = 6, NOTIFY = 7;
    private static final String[] TABS = {"接続", "会話", "ログ"};
    private static final String[] LOG_FILTERS = {"すべて", "BLE", "OpenAI", "Android", "Muse", "Irodori"};
    private static final String[] DEVICE_SECTIONS = {"会話と音量", "表示", "電源とWi-Fi"};
    private static final int[][] DEVICE_FIELDS = {{0, 1, 2, 3, 4, 5, 6}, {7, 8, 9, 10, 11}, {12, 13}};
    private final ConversationLog logs = ConversationLog.INSTANCE;
    private PhoneSettings phone;
    private ConversationService service;
    private boolean bound, updatingSwitch, resumed, connectionResumed;
    private int tab, filter, deviceSection;
    private FrameLayout content;
    private TextView device, connectionState, connectionDetail, assetStatus, openaiStatus, museStatus, modelStatus, referenceStatus;
    private View connectionDetails;
    private SegmentedBar navigation;
    private SpeechBackend.Task museCheck;
    private boolean choosingDevice;
    private LinearLayout nearby, deviceSettings;
    private Button search, checkMuseButton;
    private Switch autoConnect;
    private DeviceSettings.Snapshot visibleSettings;
    private List<ConversationService.Nearby> visibleNearby = List.of();
    private boolean visibleSettingsBusy;
    private LogAdapter logAdapter;
    private TextView logCount;
    private ListView logList;
    private List<ConversationLog.Entry> filtered = List.of();

    private final ServiceConnection connection = new ServiceConnection() {
        @Override public void onServiceConnected(ComponentName component, IBinder binder) {
            service = ((ConversationService.LocalBinder) binder).service();
            service.listen(MainActivity.this::render);
            showTab(tab);
            if (resumed) resumeConnection();
        }

        @Override public void onServiceDisconnected(ComponentName component) {
            service = null;
            render();
        }
    };

    @Override public void onCreate(Bundle state) {
        super.onCreate(state);
        phone = new PhoneSettings(this);
        tab = state == null ? 0 : state.getInt("tab");
        filter = state == null ? 0 : state.getInt("filter");
        deviceSection = state == null ? 0 : state.getInt("deviceSection");
        var root = vertical();
        root.setBackgroundColor(PAPER);
        getWindow().setStatusBarColor(Color.TRANSPARENT);
        getWindow().setNavigationBarColor(Color.TRANSPARENT);
        getWindow().getDecorView().setSystemUiVisibility(View.SYSTEM_UI_FLAG_LIGHT_STATUS_BAR |
                View.SYSTEM_UI_FLAG_LIGHT_NAVIGATION_BAR);
        root.setOnApplyWindowInsetsListener((view, insets) -> {
            var safe = insets.getInsets(WindowInsets.Type.systemBars() | WindowInsets.Type.displayCutout());
            var keyboard = insets.getInsets(WindowInsets.Type.ime());
            view.setPadding(safe.left, safe.top, safe.right, Math.max(safe.bottom, keyboard.bottom));
            return insets;
        });

        var toolbar = new LinearLayout(this);
        toolbar.setGravity(Gravity.CENTER_VERTICAL);
        toolbar.setPadding(dp(22), dp(12), dp(22), dp(8));
        TextView title = text("すみ StopWatch", 23, INK);
        title.setTypeface(Typeface.DEFAULT, Typeface.BOLD);
        toolbar.addView(title, new LinearLayout.LayoutParams(0, -2, 1));
        root.addView(toolbar);

        content = new FrameLayout(this);
        root.addView(content, new LinearLayout.LayoutParams(-1, 0, 1));

        navigation = new SegmentedBar(TABS, tab, this::showTab);
        var navigationSize = new LinearLayout.LayoutParams(-1, dp(SegmentedBar.HEIGHT_DP));
        navigationSize.setMargins(dp(20), dp(6), dp(20), dp(8));
        root.addView(navigation, navigationSize);
        setContentView(root);
        showTab(tab);
        bound = bindService(new Intent(this, ConversationService.class), connection, Context.BIND_AUTO_CREATE);
    }

    @Override protected void onResume() {
        super.onResume();
        resumed = true;
        resumeConnection();
        render();
    }

    @Override protected void onPause() {
        resumed = false;
        super.onPause();
    }

    @Override protected void onSaveInstanceState(Bundle state) {
        state.putInt("tab", tab);
        state.putInt("filter", filter);
        state.putInt("deviceSection", deviceSection);
        super.onSaveInstanceState(state);
    }

    @Override protected void onDestroy() {
        if (museCheck != null) museCheck.cancel();
        logs.listen(null);
        if (service != null) service.listen(null);
        if (bound) unbindService(connection);
        super.onDestroy();
    }

    private void resumeConnection() {
        if (service == null || connectionResumed || !phone.autoConnect() || !ConversationService.hasBluetooth(this)) return;
        if (!phone.deviceAddress().isEmpty()) {
            connectionResumed = true;
            ConversationService.resumeSaved(this);
            return;
        }
        // Migrate a single unambiguous, already bonded StopWatch from earlier app versions.
        var adapter = getSystemService(BluetoothManager.class).getAdapter();
        if (adapter == null || !adapter.isEnabled()) return;
        var watches = adapter.getBondedDevices().stream().filter(watch -> watch.getName() != null &&
                watch.getName().matches("MuseGadget-[A-Fa-f0-9]{6}")).toList();
        if (watches.size() == 1) {
            connectionResumed = true;
            BluetoothDevice watch = watches.get(0);
            startConnection(watch.getAddress(), watch.getName());
        }
    }

    private void startConnection(String address, String name) {
        phone.autoConnect(true);
        try {
            startForegroundService(new Intent(this, ConversationService.class)
                    .putExtra(ConversationService.ADDRESS, address).putExtra(ConversationService.NAME, name));
        } catch (IllegalStateException | SecurityException e) {
            toast("自動接続を開始できません。Bluetoothの権限を確認してください");
        }
        if (checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED &&
                !getPreferences(MODE_PRIVATE).getBoolean("asked_notifications", false)) {
            getPreferences(MODE_PRIVATE).edit().putBoolean("asked_notifications", true).apply();
            requestPermissions(new String[]{Manifest.permission.POST_NOTIFICATIONS}, NOTIFY);
        }
    }

    private void scan() {
        if (service == null) return;
        if (!ConversationService.hasBluetooth(this)) {
            requestPermissions(new String[]{Manifest.permission.BLUETOOTH_SCAN, Manifest.permission.BLUETOOTH_CONNECT}, BLE);
            return;
        }
        var adapter = getSystemService(BluetoothManager.class).getAdapter();
        if (adapter == null) { toast("このスマホではBluetoothを利用できません"); return; }
        if (!adapter.isEnabled()) {
            startActivityForResult(new Intent(BluetoothAdapter.ACTION_REQUEST_ENABLE), ENABLE);
            return;
        }
        service.startScan();
    }

    private void showTab(int next) {
        boolean changed = tab != next;
        tab = next;
        navigation.select(tab, changed);
        logs.listen(tab == 2 ? this::renderLogs : null);
        content.removeAllViews();
        switch (tab) {
            case 0 -> connectionScreen();
            case 1 -> conversationScreen();
            case 2 -> logScreen();
        }
        render();
        content.animate().cancel();
        content.setAlpha(changed && ValueAnimator.areAnimatorsEnabled() ? 0.65f : 1f);
        if (content.getAlpha() < 1) content.animate().alpha(1).setDuration(150).start();
    }

    private LinearLayout page() {
        var scroll = new ScrollView(this);
        scroll.setFillViewport(true);
        scroll.setClipToPadding(false);
        var body = vertical();
        body.setPadding(dp(20), dp(14), dp(20), dp(24));
        scroll.addView(body);
        content.addView(scroll, new FrameLayout.LayoutParams(-1, -1));
        return body;
    }

    private void connectionScreen() {
        var body = page();
        var panel = card(body, 0xff183e31);
        connectionState = text("", 13, 0xffbcead5);
        device = text("", 23, Color.WHITE);
        device.setTypeface(Typeface.DEFAULT, Typeface.BOLD);
        connectionDetail = text("", 15, 0xffe1f0e7);
        row(panel, connectionState, 0);
        row(panel, device, 3);
        row(panel, connectionDetail, 4);
        var summary = new LinearLayout(this);
        summary.setGravity(Gravity.CENTER_VERTICAL);
        autoConnect = new Switch(this);
        autoConnect.setText("自動接続");
        autoConnect.setTextColor(INK);
        autoConnect.setMinHeight(dp(48));
        autoConnect.setOnCheckedChangeListener((view, checked) -> {
            if (updatingSwitch || service == null) return;
            if (checked) startConnection(phone.deviceAddress(), phone.deviceName());
            else service.pauseConnection();
        });
        summary.addView(autoConnect, new LinearLayout.LayoutParams(0, dp(48), 1));
        var manage = button("デバイス", false, () -> {
            choosingDevice = !choosingDevice;
            render();
        });
        var actionSize = new LinearLayout.LayoutParams(-2, dp(48));
        actionSize.setMarginStart(dp(12));
        summary.addView(manage, actionSize);
        row(body, summary, 5);
        var details = vertical();
        connectionDetails = details;
        search = button("StopWatchを探す", true, this::scan);
        row(details, search, 0);
        nearby = vertical();
        row(details, nearby, 0);
        row(body, details, 2);
        var sections = new SegmentedBar(DEVICE_SECTIONS, deviceSection, index -> {
            deviceSection = index;
            visibleSettings = null;
            renderDeviceSettings();
        });
        var sectionSize = new LinearLayout.LayoutParams(-1, dp(SegmentedBar.HEIGHT_DP));
        sectionSize.topMargin = dp(15);
        body.addView(sections, sectionSize);
        deviceSettings = card(body, CARD);
        visibleSettings = null;
        visibleNearby = null;
        visibleSettingsBusy = false;
        renderDeviceSettings();
    }

    private void renderDeviceSettings() {
        DeviceSettings.Snapshot snapshot = service == null ? null : service.settings();
        boolean busy = service != null && service.settingsBusy();
        if (snapshot == visibleSettings && busy == visibleSettingsBusy && deviceSettings.getChildCount() != 0) return;
        visibleSettings = snapshot;
        visibleSettingsBusy = busy;
        deviceSettings.removeAllViews();
        if (snapshot == null) {
            row(deviceSettings, text("接続すると音量や会話先などを変更できます", 14, MUTED), 0);
            return;
        }
        for (int key : DEVICE_FIELDS[deviceSection]) {
            var field = DeviceSettings.FIELDS[key];
            int value = snapshot.values()[key];
            if (field.options() != null && field.options().length <= 4) {
                label(deviceSettings, field.title());
                int columns = field.options().length > 2 ? 3 : 2;
                for (int start = 0; start < field.options().length; start += columns) {
                    var choices = new LinearLayout(this);
                    choices.setGravity(Gravity.CENTER_VERTICAL);
                    for (int next = start; next < start + columns && next < field.options().length; next++) {
                        int selected = field.options()[next];
                        var choice = button(field.labels()[next], selected == value, () -> service.updateSetting(key, selected));
                        choice.setEnabled(!busy && selected != value);
                        choice.setSelected(selected == value);
                        var size = new LinearLayout.LayoutParams(0, -2, 1);
                        size.setMarginEnd(dp(5));
                        choices.addView(choice, size);
                    }
                    row(deviceSettings, choices, 5);
                }
            } else {
                deviceSlider(key, field, value, busy);
            }
        }
        var refresh = button(busy ? "保存中…" : "本体から読み直す", false, () -> service.refreshSettings());
        refresh.setEnabled(!busy);
        row(deviceSettings, refresh, 14);
    }

    private void deviceSlider(int key, DeviceSettings.Field field, int value, boolean busy) {
        int[] discrete = field.options();
        var title = text(field.title() + " · " + field.describe(value), 15, INK);
        var slider = new SeekBar(this);
        slider.setMax(discrete == null ? field.max() - field.min() : discrete.length - 1);
        int initial = value - field.min();
        if (discrete != null) {
            initial = 0;
            for (int i = 0; i < discrete.length; i++) if (discrete[i] == value) initial = i;
        }
        slider.setProgress(initial);
        slider.setEnabled(!busy);
        slider.setProgressTintList(ColorStateList.valueOf(GREEN));
        slider.setThumbTintList(ColorStateList.valueOf(GREEN));
        slider.setContentDescription(field.title());
        var save = button("適用", false, () -> {
            int progress = slider.getProgress();
            service.updateSetting(key, discrete == null ? progress + field.min() : discrete[progress]);
        });
        save.setEnabled(false);
        slider.setOnSeekBarChangeListener(new SeekBar.OnSeekBarChangeListener() {
            @Override public void onStartTrackingTouch(SeekBar bar) {}
            @Override public void onStopTrackingTouch(SeekBar bar) {}
            @Override public void onProgressChanged(SeekBar bar, int progress, boolean byUser) {
                int newValue = discrete == null ? progress + field.min() : discrete[progress];
                title.setText(field.title() + " · " + field.describe(newValue));
                save.setEnabled(!busy && newValue != value);
            }
        });
        row(deviceSettings, title, 12);
        var group = new LinearLayout(this);
        group.setGravity(Gravity.CENTER_VERTICAL);
        group.addView(slider, new LinearLayout.LayoutParams(0, dp(48), 1));
        group.addView(save);
        row(deviceSettings, group, 0);
    }

    private void conversationScreen() {
        var body = page();
        var engines = card(body, CARD);
        label(engines, "音声認識");
        if (service != null) providers(engines, "asr", phone.asr(), service.asrOptions());
        label(engines, "読み上げ");
        if (service != null) providers(engines, "tts", phone.tts(), service.ttsOptions());
        var echo = new Switch(this);
        echo.setText("テスト · 録音をそのまま再生");
        echo.setTextColor(INK);
        echo.setMinHeight(dp(48));
        echo.setChecked(phone.echo());
        echo.setOnCheckedChangeListener((view, checked) -> phone.echo(checked));
        row(engines, echo, 10);
        var openai = card(body, CARD);
        openaiStatus = text("", 13, MUTED);
        var openaiFields = disclosure(openai, "OpenAI", openaiStatus, !phone.hasCredential("openai"));
        var key = field("APIキー", true);
        row(openaiFields, key, 6);
        credentialActions(openaiFields, false, key, null);

        var muse = card(body, CARD);
        museStatus = text("", 13, MUTED);
        var museFields = disclosure(muse, "Muse", museStatus, !phone.hasCredential("muse"));
        var check = button("接続を確認", false, () -> {});
        checkMuseButton = check;
        var checkResult = text("", 14, MUTED);
        check.setOnClickListener(view -> {
            if (service == null || museCheck != null) return;
            check.setEnabled(false);
            check.setText("接続を確認中…");
            checkResult.setText("");
            museCheck = service.checkMuse(result -> {
                museCheck = null;
                checkResult.setText(result);
                check.setText("接続を確認");
                render();
                toast(result);
            });
        });
        row(museFields, check, 0);
        row(museFields, checkResult, 3);
        var token = field("端末アクセストークン", true);
        var vm = field("VM ID（任意）", false);
        row(museFields, token, 6);
        row(museFields, vm, 6);
        credentialActions(museFields, true, token, vm);
        row(museFields, button("認証ファイルを取り込む", false, () -> pick(MUSE)), 8);

        var files = card(body, CARD);
        modelStatus = text("", 14, INK);
        var fileFields = disclosure(files, "Irodori", modelStatus, !new File(getFilesDir(), IrodoriTts.MODEL).isFile());
        referenceStatus = text("", 14, INK);
        row(fileFields, text("分割GGUFは同じモデルの全パートを選択", 13, MUTED), 0);
        row(fileFields, button("モデルを選ぶ", false, () -> pick(MODEL)), 6);
        row(fileFields, referenceStatus, 12);
        row(fileFields, button("すみの参照を選ぶ", false, () -> pick(VOICE)), 6);
        assetStatus = text("", 14, GREEN);
        row(files, assetStatus, 6);
    }

    private LinearLayout disclosure(LinearLayout card, String name, TextView summary, boolean initiallyExpanded) {
        var panel = vertical();
        var header = new LinearLayout(this);
        header.setGravity(Gravity.CENTER_VERTICAL);
        var labels = vertical();
        label(labels, name);
        row(labels, summary, 3);
        header.addView(labels, new LinearLayout.LayoutParams(0, -2, 1));
        header.setMinimumHeight(dp(48));
        var toggle = button(initiallyExpanded ? "閉じる" : "開く", false, () -> {
            boolean expanded = panel.getVisibility() != View.VISIBLE;
            if (ValueAnimator.areAnimatorsEnabled()) {
                var animation = new android.transition.AutoTransition();
                animation.setDuration(150);
                android.transition.TransitionManager.beginDelayedTransition(card, animation);
            }
            panel.setVisibility(expanded ? View.VISIBLE : View.GONE);
            ((Button) header.getChildAt(1)).setText(expanded ? "閉じる" : "開く");
        });
        toggle.setContentDescription(name + "の設定を開閉");
        header.addView(toggle);
        row(card, header, 0);
        row(card, panel, 7);
        panel.setVisibility(initiallyExpanded ? View.VISIBLE : View.GONE);
        return panel;
    }

    private <T> void providers(LinearLayout parent, String kind, String current, Map<String, SpeechBackend.Provider<T>> choices) {
        var ids = new ArrayList<>(choices.keySet());
        var group = options(kind.equals("asr") ? "音声認識" : "読み上げ",
                ids.stream().map(id -> choices.get(id).label()).toArray(String[]::new));
        group.check(100 + Math.max(0, ids.indexOf(current)));
        listenOptions(group, index -> {
            String id = ids.get(index);
            phone.provider(kind, id);
            if (id.equals("android") && checkSelfPermission(Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED)
                requestPermissions(new String[]{Manifest.permission.RECORD_AUDIO}, MIC);
        });
        row(parent, group, 0);
        if (ids.size() == 1) ((RadioButton) group.getChildAt(0)).setClickable(false);
    }

    private void credentialActions(LinearLayout body, boolean muse, EditText key, EditText vm) {
        String type = muse ? "muse" : "openai";
        var actions = new LinearLayout(this);
        actions.setGravity(Gravity.END);
        actions.addView(button("消去", false, () -> new AlertDialog.Builder(this)
                .setMessage((muse ? "Muse" : "OpenAI") + "の保存済み認証情報を消去しますか？")
                .setNegativeButton("キャンセル", null).setPositiveButton("消去", (dialog, which) -> {
                    if (saveCredential(muse, "")) { key.setText(""); render(); }
                    else key.setError("消去できませんでした");
                }).show()));
        var save = button("保存", true, () -> {
            String value = key.getText().toString().strip();
            if (muse) {
                if (!MuseChat.isAccessToken(value)) { key.setError("Muse端末のアクセストークンを入力"); return; }
                String id = vm.getText().toString().strip();
                if (!id.matches("[A-Za-z0-9_-]{0,128}")) { vm.setError("VM IDを確認"); return; }
                try { value = new JSONObject().put("token", value).put("vm", id).toString(); }
                catch (Exception e) { key.setError("認証情報を保存できませんでした"); return; }
            } else if (!value.matches("sk-[!-~]{7,16381}")) { key.setError("OpenAI APIキーを確認"); return; }
            if (!saveCredential(muse, value)) { key.setError("認証情報を保存できませんでした"); return; }
            key.setText("");
            render();
            String label = type.equals("muse") ? "Muse" : "OpenAI";
            toast(label + "の認証情報を保存しました");
        });
        var size = new LinearLayout.LayoutParams(-2, -2);
        size.setMargins(dp(8), 0, 0, 0);
        actions.addView(save, size);
        row(body, actions, 8);
    }

    private boolean saveCredential(boolean muse, String value) {
        return muse ? phone.saveMuseCredential(value) : phone.saveOpenAiKey(value);
    }

    private void logScreen() {
        var body = vertical();
        body.setPadding(dp(20), dp(12), dp(20), dp(8));
        content.addView(body, new FrameLayout.LayoutParams(-1, -1));
        var text = new Switch(this);
        text.setText("送信・返信テキストを記録");
        text.setTextColor(INK);
        text.setMinHeight(dp(48));
        text.setChecked(phone.logText());
        text.setOnCheckedChangeListener((view, checked) -> {
            phone.logText(checked);
            logs.includeText(checked);
        });
        row(body, text, 0);

        var filters = new android.widget.HorizontalScrollView(this);
        filters.setHorizontalScrollBarEnabled(false);
        var choices = new LinearLayout(this);
        for (int i = 0; i < LOG_FILTERS.length; i++) {
            int index = i;
            var chip = button(LOG_FILTERS[i], i == filter, () -> {
                filter = index;
                for (int j = 0; j < choices.getChildCount(); j++)
                    style((Button) choices.getChildAt(j), j == filter);
                renderLogs();
            });
            var size = new LinearLayout.LayoutParams(-2, dp(48));
            size.setMarginEnd(dp(6));
            choices.addView(chip, size);
        }
        filters.addView(choices);
        row(body, filters, 2);

        var actions = new LinearLayout(this);
        actions.setGravity(Gravity.CENTER_VERTICAL);
        logCount = text("", 13, MUTED);
        actions.addView(logCount, new LinearLayout.LayoutParams(0, -2, 1));
        actions.addView(button("コピー", false, this::copyLogs));
        actions.addView(button("消去", false, logs::clear));
        row(body, actions, 4);

        logList = new ListView(this);
        logList.setDivider(null);
        logList.setDividerHeight(dp(5));
        logAdapter = new LogAdapter();
        logList.setAdapter(logAdapter);
        body.addView(logList, new LinearLayout.LayoutParams(-1, 0, 1));
        renderLogs();
    }

    private void renderLogs() {
        if (tab != 2 || logAdapter == null) return;
        var entries = new ArrayList<>(logs.snapshot().stream().filter(entry ->
                filter == 0 || entry.source().equals(LOG_FILTERS[filter])).toList());
        Collections.reverse(entries);
        var first = logList.getFirstVisiblePosition();
        var item = logList.getChildAt(0);
        int top = item == null ? 0 : item.getTop();
        int anchor = first == 0 || first >= filtered.size() ? 0 : entries.indexOf(filtered.get(first));
        filtered = entries;
        logCount.setText(entries.isEmpty() ? "ログはまだありません" : entries.size() + "件 · 新しい順");
        logAdapter.notifyDataSetChanged();
        if (anchor > 0) logList.setSelectionFromTop(anchor, top);
    }

    private void copyLogs() {
        if (filtered.isEmpty()) return;
        var rows = new ArrayList<String>();
        var time = new SimpleDateFormat("HH:mm:ss.SSS", Locale.JAPAN);
        int length = 0;
        for (var entry : filtered) {
            String row = time.format(new Date(entry.time())) + "  " + entry.source() + "  " + entry.message();
            if (length + row.length() > 180_000) { rows.add("…"); break; }
            rows.add(row);
            length += row.length() + 1;
        }
        var clip = ClipData.newPlainText("StopWatch デバッグログ", String.join("\n", rows));
        var flags = new android.os.PersistableBundle();
        flags.putBoolean("android.content.extra.IS_SENSITIVE", true);
        clip.getDescription().setExtras(flags);
        getSystemService(ClipboardManager.class).setPrimaryClip(clip);
        toast("表示中のログをコピーしました");
    }

    private final class LogAdapter extends BaseAdapter {
        private final SimpleDateFormat clock = new SimpleDateFormat("HH:mm:ss.SSS", Locale.JAPAN);
        @Override public int getCount() { return filtered.size(); }
        @Override public Object getItem(int index) { return filtered.get(index); }
        @Override public long getItemId(int index) { return index; }

        @Override public View getView(int index, View previous, ViewGroup parent) {
            var entry = filtered.get(index);
            LinearLayout item;
            if (previous instanceof LinearLayout old) item = old;
            else {
                item = vertical();
                item.setPadding(dp(14), dp(10), dp(14), dp(10));
                item.setBackground(shape(CARD, 12, 0));
                row(item, text("", 12, GREEN), 0);
                var message = text("", 14, INK);
                message.setTextIsSelectable(true);
                row(item, message, 4);
            }
            ((TextView) item.getChildAt(0)).setText(clock.format(new Date(entry.time())) + "   " + entry.source());
            ((TextView) item.getChildAt(1)).setText(entry.message());
            return item;
        }
    }

    private void render() {
        if (tab == 0) {
            connectionState.setText(service == null ? "未接続" : service.link());
            device.setText(service == null ? phone.deviceName() : service.name());
            connectionDetail.setText(service == null ? "StopWatchを登録すると自動で接続します" : service.status());
            boolean registered = !phone.deviceAddress().isEmpty();
            connectionDetails.setVisibility(!registered || choosingDevice ? View.VISIBLE : View.GONE);
            updatingSwitch = true;
            autoConnect.setEnabled(registered && service != null);
            autoConnect.setChecked(registered && phone.autoConnect());
            updatingSwitch = false;
            search.setEnabled(service != null && !service.searching());
            search.setText(service != null && service.searching() ? "検索中…" : registered ? "別のStopWatchを探す" : "StopWatchを探す");
            var found = service == null ? List.<ConversationService.Nearby>of() : service.nearby();
            if (!found.equals(visibleNearby)) {
                nearby.removeAllViews();
                for (var watch : found) row(nearby, button(watch.name() + " に接続", true,
                        () -> startConnection(watch.address(), watch.name())), 8);
                visibleNearby = found;
            }
            renderDeviceSettings();
        } else if (tab == 1) {
            openaiStatus.setText(phone.hasCredential("openai") ? "APIキーを保存済み" : "APIキー未設定");
            museStatus.setText(phone.hasCredential("muse") ? "端末認証情報を保存済み" : "端末認証未設定");
            checkMuseButton.setEnabled(service != null && museCheck == null && phone.hasCredential("muse"));
            var installed = IrodoriModels.bySize(new File(getFilesDir(), IrodoriTts.MODEL).length());
            modelStatus.setText(installed == null ? "モデル未設定" : installed.name());
            var voice = installed != null && installed.anime() ? IrodoriTts.ANIME_VOICE : IrodoriTts.VOICE;
            referenceStatus.setText(new File(getFilesDir(), voice).isFile() ? "すみの参照 · 取り込み済み" : "すみの参照 · 未設定");
            assetStatus.setText(service == null ? "" : service.assets());
            assetStatus.setVisibility(assetStatus.length() == 0 ? View.GONE : View.VISIBLE);
        }
    }

    private void pick(int request) {
        Intent picker = new Intent(Intent.ACTION_OPEN_DOCUMENT).setType("*/*").addCategory(Intent.CATEGORY_OPENABLE);
        if (request == MODEL) picker.putExtra(Intent.EXTRA_ALLOW_MULTIPLE, true);
        startActivityForResult(picker, request);
    }

    @Override public void onRequestPermissionsResult(int request, String[] permissions, int[] results) {
        super.onRequestPermissionsResult(request, permissions, results);
        if (request == BLE && ConversationService.hasBluetooth(this)) scan();
        else if (request == BLE) toast("接続には「付近のデバイス」の許可が必要です");
        if (request == MIC && (results.length == 0 || results[0] != PackageManager.PERMISSION_GRANTED))
            toast("Android音声認識にはマイクの権限が必要です");
    }

    @Override protected void onActivityResult(int request, int result, Intent data) {
        super.onActivityResult(request, result, data);
        if (result != RESULT_OK || service == null) return;
        if (request == ENABLE) { scan(); return; }
        if (data == null) return;
        if (request == MODEL) {
            var selected = new ArrayList<Uri>();
            var clip = data.getClipData();
            if (clip != null) for (int i = 0; i < clip.getItemCount(); i++) {
                Uri uri = clip.getItemAt(i).getUri();
                if (uri != null) selected.add(uri);
            }
            if (selected.isEmpty() && data.getData() != null) selected.add(data.getData());
            if (!selected.isEmpty()) service.importModel(selected);
        } else if (request == VOICE && data.getData() != null) service.importVoice(data.getData());
        else if (request == MUSE && data.getData() != null) service.importMuse(data.getData());
    }

    private RadioGroup options(String title, String[] labels) {
        var options = new RadioGroup(this);
        options.setOrientation(LinearLayout.VERTICAL);
        options.setContentDescription(title);
        for (int i = 0; i < labels.length; i++) {
            var radio = new RadioButton(this);
            radio.setId(100 + i);
            radio.setText(labels[i]);
            radio.setTextColor(INK);
            radio.setButtonTintList(ColorStateList.valueOf(GREEN));
            radio.setMinHeight(dp(48));
            options.addView(radio);
        }
        return options;
    }

    private void listenOptions(RadioGroup group, IntConsumer selected) {
        group.setOnCheckedChangeListener((view, checked) -> { if (checked >= 100) selected.accept(checked - 100); });
    }

    private EditText field(String hint, boolean secret) {
        var field = new EditText(this);
        field.setHint(hint);
        field.setSingleLine();
        field.setTextSize(15);
        field.setTextColor(INK);
        field.setSaveEnabled(!secret);
        field.setInputType(InputType.TYPE_CLASS_TEXT | (secret ? InputType.TYPE_TEXT_VARIATION_PASSWORD : 0));
        field.setPadding(dp(12), dp(10), dp(12), dp(10));
        field.setBackground(shape(PAPER, 10, LINE));
        field.setMinHeight(dp(48));
        return field;
    }

    private LinearLayout card(LinearLayout parent, int fill) {
        var card = vertical();
        card.setPadding(dp(16), dp(14), dp(16), dp(14));
        card.setBackground(shape(fill, 18, fill == CARD ? LINE : 0));
        row(parent, card, 10);
        return card;
    }

    private void label(LinearLayout parent, String value) {
        var title = text(value, 15, INK);
        title.setTypeface(Typeface.DEFAULT, Typeface.BOLD);
        row(parent, title, parent.getChildCount() == 0 ? 0 : 10);
    }

    private Button button(String value, boolean primary, Runnable action) {
        var button = new Button(this);
        button.setText(value);
        button.setAllCaps(false);
        button.setTextSize(14);
        button.setMinHeight(dp(48));
        button.setMinimumHeight(dp(48));
        button.setMinWidth(dp(48));
        button.setMinimumWidth(dp(48));
        button.setPadding(dp(12), 0, dp(12), 0);
        button.setOnClickListener(view -> action.run());
        style(button, primary);
        return button;
    }

    private void style(Button button, boolean primary) {
        button.setTextColor(primary ? Color.WHITE : INK);
        button.setBackground(new android.graphics.drawable.RippleDrawable(ColorStateList.valueOf(0x2876a68b),
                shape(primary ? GREEN : PALE, 12, 0), null));
    }

    private final class SegmentedBar extends FrameLayout {
        static final int HEIGHT_DP = 56;
        private static final int INSET_DP = 4;
        private final View indicator;
        private final Button[] choices;
        private int selected;

        SegmentedBar(String[] labels, int selected, IntConsumer onSelect) {
            super(MainActivity.this);
            this.selected = selected;
            int inset = dp(INSET_DP);
            setPadding(inset, inset, inset, inset);
            setBackground(shape(0xffe8ede7, 22, 0));
            indicator = new View(MainActivity.this);
            indicator.setBackground(shape(GREEN, 18, 0));
            indicator.setImportantForAccessibility(View.IMPORTANT_FOR_ACCESSIBILITY_NO);
            int itemHeight = dp(HEIGHT_DP) - 2 * inset;
            addView(indicator, new FrameLayout.LayoutParams(0, itemHeight));
            var row = new LinearLayout(MainActivity.this);
            choices = new Button[labels.length];
            for (int i = 0; i < labels.length; i++) {
                int index = i;
                var choice = button(labels[i], false, () -> {
                    if (this.selected == index) return;
                    select(index, true);
                    onSelect.accept(index);
                });
                choice.setBackground(new android.graphics.drawable.RippleDrawable(
                        ColorStateList.valueOf(0x3076a68b), null, shape(CARD, 18, 0)));
                choices[i] = choice;
                row.addView(choice, new LinearLayout.LayoutParams(0, itemHeight, 1));
            }
            addView(row, new FrameLayout.LayoutParams(-1, -2));
            select(selected, false);
        }

        @Override protected void onLayout(boolean changed, int left, int top, int right, int bottom) {
            super.onLayout(changed, left, top, right, bottom);
            if (changed) {
                int start = getPaddingLeft();
                int width = Math.max(0, (getWidth() - start - getPaddingRight()) / choices.length);
                indicator.layout(start, getPaddingTop(), start + width, getHeight() - getPaddingBottom());
                indicator.setTranslationX(selected * width);
            }
        }

        void select(int next, boolean animate) {
            selected = next;
            for (int i = 0; i < choices.length; i++) {
                choices[i].setSelected(i == next);
                choices[i].setTextColor(i == next ? Color.WHITE : INK);
            }
            float x = indicator.getWidth() * next;
            indicator.animate().cancel();
            if (animate && ValueAnimator.areAnimatorsEnabled()) indicator.animate().translationX(x)
                    .setDuration(210).setInterpolator(new DecelerateInterpolator()).start();
            else indicator.setTranslationX(x);
        }
    }

    private GradientDrawable shape(int fill, int radius, int stroke) {
        var shape = new GradientDrawable();
        shape.setColor(fill);
        shape.setCornerRadius(dp(radius));
        if (stroke != 0) shape.setStroke(dp(1), stroke);
        return shape;
    }

    private TextView text(String value, int size, int color) {
        var text = new TextView(this);
        text.setText(value);
        text.setTextSize(size);
        text.setTextColor(color);
        return text;
    }

    private LinearLayout vertical() {
        var result = new LinearLayout(this);
        result.setOrientation(LinearLayout.VERTICAL);
        return result;
    }

    private void row(LinearLayout parent, View child, int top) {
        var size = new LinearLayout.LayoutParams(-1, -2);
        size.topMargin = dp(top);
        parent.addView(child, size);
    }

    private void toast(String message) { Toast.makeText(this, message, Toast.LENGTH_SHORT).show(); }
    private int dp(float value) { return Math.round(value * getResources().getDisplayMetrics().density); }
}
