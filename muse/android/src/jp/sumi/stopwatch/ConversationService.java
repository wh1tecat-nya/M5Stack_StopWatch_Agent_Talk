package jp.sumi.stopwatch;

import android.Manifest;
import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.bluetooth.BluetoothAdapter;
import android.bluetooth.BluetoothDevice;
import android.bluetooth.BluetoothGatt;
import android.bluetooth.BluetoothGattCallback;
import android.bluetooth.BluetoothGattCharacteristic;
import android.bluetooth.BluetoothGattDescriptor;
import android.bluetooth.BluetoothGattService;
import android.bluetooth.BluetoothManager;
import android.bluetooth.BluetoothProfile;
import android.bluetooth.BluetoothStatusCodes;
import android.bluetooth.le.ScanCallback;
import android.bluetooth.le.ScanFilter;
import android.bluetooth.le.ScanResult;
import android.bluetooth.le.ScanSettings;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.content.pm.PackageManager;
import android.content.pm.ServiceInfo;
import android.net.Uri;
import android.os.Binder;
import android.os.Handler;
import android.os.IBinder;
import android.os.Looper;
import android.os.ParcelUuid;
import android.os.PowerManager;
import android.os.SystemClock;
import android.provider.OpenableColumns;
import android.util.Log;

import java.io.File;
import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.StandardCopyOption;
import java.security.MessageDigest;
import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.function.Consumer;
import java.util.function.IntConsumer;
import java.util.function.IntSupplier;

public final class ConversationService extends Service implements StopwatchAudio.Link {
    private static final UUID ADVERTISEMENT = UUID.fromString("7fdd3d1c-38ea-46cf-8b46-314ecf5f240c");
    private static final UUID SERVICE = UUID.fromString("4d757365-0011-4000-8000-6a6f6c6c7900");
    private static final UUID RX = UUID.fromString("4d757365-0012-4000-8000-6a6f6c6c7900");
    private static final UUID TX = UUID.fromString("4d757365-0013-4000-8000-6a6f6c6c7900");
    private static final UUID SETTINGS = UUID.fromString("4d757365-0014-4000-8000-6a6f6c6c7900");
    private static final UUID CCCD = UUID.fromString("00002902-0000-1000-8000-00805f9b34fb");
    private static final int WRITE = 1, READ = 2, DESCRIPTOR = 3;
    private static final int NOTIFICATION_ID = 152;
    private static final int CHAT_SYNC_INTERVAL = 4_000;
    private static final String CHAT_CATALOG_REQUEST = """
            この依頼はStopWatchの会話先一覧を取得するためだけの一時的な処理です。
            この依頼内容と取得結果を、今後のコンテキストとして記憶しないでください。
            メモリ、プロフィール、ノート、ファイルへの作成・追記・更新は一切行わないでください。
            あなたがアクセスできるチャット一覧取得機能を使って、現在存在するアーカイブされていない
            サイドチャットのタイトルと正確な session_id を漏れなく取得してください。
            メインチャットは含めず、IDや名前を推測しないでください。説明やMarkdownなしでJSONだけを返してください。
            取得が完了した場合は {"request_id":"%s","complete":true,"chats":[{"session_id":"UUID","title":"表示名"}]}。
            0件の場合は chats を空配列にしてください。一覧を完全に取得できない場合は
            {"request_id":"%s","complete":false,"chats":[]} を返してください。
            request_id はこの依頼と応答を照合するための値です。例の値をそのまま返してください。
            """;
    private static final String CHANNEL = "stopwatch_connection";
    private static final String CONNECT = "jp.sumi.stopwatch.CONNECT";
    static final String ADDRESS = "address", NAME = "name";
    private record Operation(int type, IntSupplier start, Consumer<byte[]> complete, IntConsumer failure) {
        Operation(int type, IntSupplier start, Consumer<byte[]> complete) {
            this(type, start, complete, null);
        }
    }
    record Nearby(String address, String name) {}
    private final ConversationLog log = ConversationLog.INSTANCE;
    private final Map<String, Nearby> nearby = new LinkedHashMap<>();
    private final Binder binder = new LocalBinder();
    private Runnable listener;
    private String target = "", name = "", link = "未接続";
    private String status = "StopWatchを登録すると自動で接続します";
    private String assets = "";
    private DeviceSettings.Snapshot snapshot;
    private ChatCatalog chatCatalog;
    private ChatCatalog.Watch watchChats;
    private SpeechBackend.Task catalogRequest;
    private boolean catalogSyncBusy, catalogUnsupported;
    private String catalogStatus = "";
    private String catalogLinkProblem = "";
    private final Runnable syncCatalog = this::syncChatCatalog;
    private PowerManager.WakeLock wake;
    private boolean foreground, wanted, settingPending;
    private long reconnectDelay = 1_000;
    private final Runnable reconnect = () -> open(false);
    private final BroadcastReceiver bluetoothState = new BroadcastReceiver() {
        @Override public void onReceive(Context context, Intent intent) {
            int state = intent.getIntExtra(BluetoothAdapter.EXTRA_STATE, BluetoothAdapter.ERROR);
            if (state == BluetoothAdapter.STATE_OFF || state == BluetoothAdapter.STATE_TURNING_OFF) {
                closeLink();
                update("Bluetoothをオンにすると再接続します", "Bluetoothオフ");
            } else if (state == BluetoothAdapter.STATE_ON && wanted) open(false);
        }
    };
    final class LocalBinder extends Binder { ConversationService service() { return ConversationService.this; } }

    private final Handler handler = new Handler(Looper.getMainLooper());
    private final ArrayDeque<Operation> operations = new ArrayDeque<>();
    private BluetoothAdapter adapter;
    private BluetoothGatt gatt;
    private BluetoothGattCharacteristic rx, tx, settingsCharacteristic;
    private int mtu = 23;
    private final ExecutorService workers = Executors.newCachedThreadPool();
    private final ExecutorService inference = Executors.newSingleThreadExecutor();
    private PhoneSettings phone;
    private IrodoriTts irodori;
    private MuseChat muse;
    private SpeechBackend.Task museVerification;
    private MuseAccessSync.Download museDownload;
    private byte[] museUpload;
    private MusePair managedPair;
    private Consumer<String> managedCompletion;
    private final java.util.Set<String> rotationsInFlight = new java.util.HashSet<>();
    private final java.util.Map<Long, Long> refreshBackoff = new java.util.HashMap<>();
    private final Runnable retryManagedPair = () -> syncMusePair(null, null);
    private final Runnable pollMode = this::pollDeviceMode;
    private final Runnable updateReplyWatch = this::updateReplyWatch;
    private final ArrayDeque<String> externalReplies = new ArrayDeque<>();
    private final LinkedHashSet<String> deliveredReplies = new LinkedHashSet<>();
    private SpeechBackend.Task replyWatch;
    private String watchedSession;
    private long watchGeneration, watchRetryAt;
    private long watchRetryDelay = 2_000;
    private final Runnable probeFirmware = this::probeFirmware;
    private final Runnable firmwareBootTimeout = () -> finishFirmware("更新後の起動を確認できませんでした");
    private C152Ota.Info firmwareInfo;
    private boolean firmwareProbeBusy, firmwareUnsupported, readingFirmware;
    private String firmwareMessage = "";
    private FirmwareTransfer firmware;

    private static final class FirmwareTransfer {
        final C152Ota.Image image;
        final C152Ota.Upload upload;
        final int oldSlot;
        final long started = SystemClock.elapsedRealtime();
        int offset, percent = -1;
        long verifyAfter;
        boolean disconnected;
        FirmwareTransfer(C152Ota.Image image, C152Ota.Upload upload, int oldSlot) {
            this.image = image;
            this.upload = upload;
            this.oldSlot = oldSlot;
        }
        boolean rebooting() { return verifyAfter != 0; }
    }
    private String museSyncStatus = "";
    private boolean museSyncBusy;
    private final Map<String, SpeechBackend.Provider<SpeechBackend.Asr>> asrBackends = new LinkedHashMap<>();
    private final Map<String, SpeechBackend.Provider<SpeechBackend.Tts>> ttsBackends = new LinkedHashMap<>();
    private SpeechPipeline pipeline;
    private boolean speechReady;
    private int settingsSequence = (int) (System.nanoTime() & 0xffff);
    private StopwatchAudio audio;
    private ScanCallback scan;
    private Operation running;
    private final Runnable operationTimeout = () -> disconnect("StopWatchの応答がありません");

    static boolean hasBluetooth(Context context) {
        return context.checkSelfPermission(Manifest.permission.BLUETOOTH_CONNECT) == PackageManager.PERMISSION_GRANTED &&
                context.checkSelfPermission(Manifest.permission.BLUETOOTH_SCAN) == PackageManager.PERMISSION_GRANTED;
    }

    static void resumeSaved(Context context) {
        PhoneSettings options = new PhoneSettings(context);
        if (options.autoConnect() && !options.deviceAddress().isEmpty() && hasBluetooth(context)) {
            try { context.startForegroundService(new Intent(context, ConversationService.class).setAction(CONNECT)); }
            catch (IllegalStateException | SecurityException e) {
                ConversationLog.INSTANCE.event(ConversationLog.BLE, "自動接続の開始をAndroidが制限しました");
            }
        }
    }

    @Override public void onCreate() {
        super.onCreate();
        adapter = getSystemService(BluetoothManager.class).getAdapter();
        registerReceiver(bluetoothState, new IntentFilter(BluetoothAdapter.ACTION_STATE_CHANGED), Context.RECEIVER_EXPORTED);
        wake = getSystemService(PowerManager.class).newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "sumi:conversation");
        wake.setReferenceCounted(false);
        phone = new PhoneSettings(this);
        chatCatalog = phone.chatCatalog();
        log.includeText(phone.logText());
        irodori = new IrodoriTts(this, inference);
        var openai = new OpenAiAsr(phone::openAiKey, workers);
        var android = new AndroidAsr(this, handler, workers);
        muse = new MuseChat(this::museChatCredential, workers);
        asrBackends.put("openai", new SpeechBackend.Provider<>("OpenAI gpt-transcribe", openai));
        asrBackends.put("android", new SpeechBackend.Provider<>("Android 端末内", android));
        ttsBackends.put("irodori", new SpeechBackend.Provider<>("Irodori-TTS", irodori));
        pipeline = new SpeechPipeline(handler,
                () -> new SpeechBackend.Providers(asrBackends.getOrDefault(phone.asr(), asrBackends.get("openai")).backend(),
                        this::managedReply, ttsBackends.getOrDefault(phone.tts(), ttsBackends.get("irodori")).backend()),
                this::stage, workers);
        audio = new StopwatchAudio(this, new StopwatchAudio.Speech() {
            @Override public void start(StopwatchAudio.Recording note, StopwatchAudio.Reply reply) {
                if (phone.echo()) reply.play(note.pcm());
                else pipeline.start(note, reply);
            }
            @Override public void cancel() { pipeline.cancel(); }
        }, this::stage, active -> {
            if (active) {
                wake.acquire(210_000);
                if (!phone.echo()) {
                    ttsBackends.getOrDefault(phone.tts(), ttsBackends.get("irodori")).backend().warmUp();
                }
            } else if (wake.isHeld()) wake.release();
            scheduleReplyWatch();
        });
    }

    @Override public IBinder onBind(Intent intent) { return binder; }

    @Override public int onStartCommand(Intent intent, int flags, int id) {
        if (!hasBluetooth(this)) { stopSelf(id); return START_NOT_STICKY; }
        String address = intent == null ? null : intent.getStringExtra(ADDRESS);
        String selected = intent == null ? null : intent.getStringExtra(NAME);
        if (address == null) address = phone.deviceAddress();
        if (selected == null) selected = phone.deviceName();
        if (!BluetoothAdapter.checkBluetoothAddress(address) || !phone.autoConnect()) {
            stopSelf(id);
            return START_NOT_STICKY;
        }
        if (!address.equals(target)) closeLink();
        name = selected;
        target = address;
        wanted = true;
        stopScan();
        nearby.clear();
        NotificationManager manager = getSystemService(NotificationManager.class);
        manager.createNotificationChannel(new NotificationChannel(CHANNEL, "StopWatchの接続",
                NotificationManager.IMPORTANCE_LOW));
        startForeground(NOTIFICATION_ID, notification(), ServiceInfo.FOREGROUND_SERVICE_TYPE_CONNECTED_DEVICE);
        foreground = true;
        if (gatt == null) open(true);
        return START_STICKY;
    }

    @Override public void onDestroy() {
        listener = null;
        wanted = false;
        closeLink();
        if (catalogRequest != null) catalogRequest.cancel();
        unregisterReceiver(bluetoothState);
        handler.removeCallbacksAndMessages(null);
        irodori.close();
        inference.shutdown();
        workers.shutdownNow();
        super.onDestroy();
    }

    void listen(Runnable next) { listener = next; }
    String name() { return name.isEmpty() ? phone.deviceName() : name; }
    String link() { return link; }
    String status() { return status; }
    String assets() { return assets; }
    boolean searching() { return scan != null; }
    boolean settingsBusy() { return settingPending || museSyncBusy || firmwareProbeBusy || firmware != null; }
    boolean museManaged() { return managedPair != null; }
    DeviceSettings.Snapshot settings() { return snapshot; }
    List<Nearby> nearby() { return List.copyOf(nearby.values()); }
    Map<String, SpeechBackend.Provider<SpeechBackend.Asr>> asrOptions() { return asrBackends; }
    Map<String, SpeechBackend.Provider<SpeechBackend.Tts>> ttsOptions() { return ttsBackends; }

    ChatCatalog chatCatalog() { return chatCatalog; }
    boolean catalogRefreshing() { return catalogRequest != null; }
    boolean canRefreshCatalog() { return firmware == null && !catalogRefreshing() && !wake.isHeld() && (museManaged() || phone.hasCredential("muse")); }
    String catalogStatus() { return catalogStatus; }

    String catalogLinkStatus() {
        if (catalogUnsupported) return "一覧の同期にはC152の更新が必要";
        if (!catalogLinkProblem.isEmpty()) return catalogLinkProblem;
        if (snapshot == null) return "BLE接続時にC152と自動同期";
        return selectedChatId() != null ? "C152と同期済み" : "C152への同期を待機中";
    }

    String selectedChatId() {
        if (watchChats == null) {
            if (catalogUnsupported && chatCatalog.equals(ChatCatalog.DEFAULT) && snapshot != null)
                return snapshot.values()[1] == 0 ? "" : ChatCatalog.LEGACY_ID;
            return null;
        }
        List<ChatCatalog.Chat> rows = chatCatalog.visible();
        return watchChats.hash() == ChatCatalog.hash(chatCatalog.watchData()) && watchChats.selected() < rows.size()
                ? rows.get(watchChats.selected()).id() : null;
    }

    boolean canSelectChats() { return selectedChatId() != null && !settingsBusy(); }

    void refreshChatCatalog() {
        if (!canRefreshCatalog()) return;
        stopReplyWatch();
        String session = selectedChatId();
        if (session == null) session = "";
        String id = UUID.randomUUID().toString();
        var call = new SpeechBackend.ChatRequest(CHAT_CATALOG_REQUEST.formatted(id, id), session, false, id);
        catalogRequest = managedReply(call,
                SpeechBackend.result(value -> handler.post(() -> {
                    catalogRequest = null;
                    try { saveChatCatalog(chatCatalog.refreshed(value)); }
                    catch (org.json.JSONException e) {
                        log.event(ConversationLog.MUSE, "チャット一覧 · 完全な一覧として検証できませんでした");
                        catalogStatus = "Museから完全な一覧を取得できませんでした";
                        changed();
                    }
                }), error -> handler.post(() -> {
                    catalogRequest = null;
                    catalogStatus = error;
                    changed();
                })));
        catalogStatus = "Museにチャット一覧を確認中…";
        changed();
    }

    void editChat(String id, boolean hide) {
        if (ChatCatalog.isSideId(id)) saveChatCatalog(chatCatalog.edit(id, hide));
    }

    void showChat(String id) {
        if (chatCatalog.hidden().containsKey(id)) saveChatCatalog(chatCatalog.show(id));
    }

    private void saveChatCatalog(ChatCatalog next) {
        if (!phone.saveChatCatalog(next)) {
            catalogStatus = "チャット一覧を保存できませんでした";
        } else {
            chatCatalog = next;
            catalogStatus = "チャット一覧を保存しました";
            scheduleChatCatalog(0);
        }
        changed();
    }

    void selectChat(String id) {
        if (!canSelectChats()) return;
        if (catalogSyncBusy) {
            BluetoothGatt connection = gatt;
            handler.postDelayed(() -> { if (gatt == connection) selectChat(id); }, 120);
            return;
        }
        List<ChatCatalog.Chat> rows = chatCatalog.visible();
        for (int i = 0; i < rows.size(); i++) {
            if (!rows.get(i).id().equals(id)) continue;
            updateSetting(1, i);
            return;
        }
    }

    private void scheduleChatCatalog(long delay) {
        handler.removeCallbacks(syncCatalog);
        if (gatt != null && snapshot != null && !catalogUnsupported) handler.postDelayed(syncCatalog, delay);
    }

    private void syncChatCatalog() {
        BluetoothGatt connection = gatt;
        if (connection == null || snapshot == null || catalogUnsupported) return;
        if (settingsBusy() || catalogSyncBusy || wake.isHeld()) { scheduleChatCatalog(CHAT_SYNC_INTERVAL); return; }
        catalogSyncBusy = true;
        readChatCatalog(connection, current -> {
            byte[] data = chatCatalog.watchData();
            int hash = ChatCatalog.hash(data);
            if (current.hash() == hash) finishChatCatalog(current);
            else writeCatalog(connection, ChatCatalog.command(ChatCatalog.BEGIN, data.length),
                    () -> uploadChatCatalog(connection, data, 0, hash));
        });
    }

    private void readChatCatalog(BluetoothGatt connection, Consumer<ChatCatalog.Watch> result) {
        writeCatalog(connection, ChatCatalog.command(ChatCatalog.STATUS, 0), () ->
                enqueue(new Operation(READ,
                        () -> connection.readCharacteristic(settingsCharacteristic)
                                ? BluetoothStatusCodes.SUCCESS : BluetoothStatusCodes.ERROR_UNKNOWN,
                        bytes -> {
                            ChatCatalog.Watch state;
                            try { state = ChatCatalog.Watch.read(bytes); }
                            catch (IllegalArgumentException e) { chatCatalogFailure(0); return; }
                            result.accept(state);
                        }, this::chatCatalogFailure)));
    }

    private void uploadChatCatalog(BluetoothGatt connection, byte[] data, int offset, int hash) {
        if (offset == data.length) {
            writeCatalog(connection, ChatCatalog.command(ChatCatalog.COMMIT, 0), () ->
                    readChatCatalog(connection, applied -> {
                        if (applied.hash() == hash) finishChatCatalog(applied);
                        else chatCatalogFailure(0);
                    }));
            return;
        }
        byte[] packet = ChatCatalog.part(data, offset, mtu);
        writeCatalog(connection, packet, () -> uploadChatCatalog(connection, data, offset + packet.length - 4, hash));
    }

    private void writeCatalog(BluetoothGatt connection, byte[] packet, Runnable done) {
        enqueue(new Operation(WRITE, () -> connection.writeCharacteristic(settingsCharacteristic, packet,
                BluetoothGattCharacteristic.WRITE_TYPE_DEFAULT), unused -> done.run(), this::chatCatalogFailure));
    }

    private void finishChatCatalog(ChatCatalog.Watch current) {
        catalogSyncBusy = false;
        boolean different = !current.equals(watchChats) || !catalogLinkProblem.isEmpty();
        catalogLinkProblem = "";
        watchChats = current;
        if (different) changed();
        else scheduleReplyWatch();
        scheduleChatCatalog(CHAT_SYNC_INTERVAL);
    }

    private void chatCatalogFailure(int code) {
        catalogSyncBusy = false;
        if (code == 0x0d || code == 0x0e) {
            catalogUnsupported = true;
        } else if (code != 0x81) catalogLinkProblem = "C152との一覧同期を再試行中";
        changed();
        scheduleChatCatalog(CHAT_SYNC_INTERVAL);
    }

    SpeechBackend.Task checkMuse(Consumer<String> result) {
        return managedReply(null, SpeechBackend.result(
                value -> handler.post(() -> result.accept(value)),
                error -> handler.post(() -> result.accept(error))));
    }

    private String museChatCredential() {
        String saved = phone.museCredential();
        if (managedPair == null) return saved;
        try {
            var fields = saved.isEmpty() ? new org.json.JSONObject() : new org.json.JSONObject(saved);
            return fields.put("token", managedPair.access()).toString();
        } catch (org.json.JSONException e) { return saved; }
    }

    private SpeechBackend.Task managedReply(SpeechBackend.ChatRequest call, SpeechBackend.Result<String> result) {
        final class Attempt implements SpeechBackend.Task {
            private SpeechBackend.Task network;
            private volatile boolean cancelled;

            void start(boolean retry) {
                if (cancelled) return;
                String tried = managedPair == null ? "" : managedPair.access();
                var receive = SpeechBackend.<String>result(result::success, error -> {
                    if (retry && MuseChat.accessRejected(error)) handler.post(() -> {
                        if (cancelled) return;
                        if (snapshot == null) { result.failure(error); return; }
                        if (snapshot.values()[0] != 1) {
                            if (call != null && call.catalog()) syncMusePair(null, problem -> {
                                if (cancelled) return;
                                if (problem == null) start(false);
                                else result.failure(problem);
                            });
                            else result.failure(error);
                            return;
                        }
                        syncMusePair(tried, problem -> {
                            if (cancelled) return;
                            if (problem == null) start(tried.isEmpty());
                            else result.failure(problem);
                        });
                    });
                    else if (!cancelled) result.failure(error);
                });
                network = call == null ? muse.check(receive) : muse.reply(call, receive);
            }

            @Override public void cancel() {
                cancelled = true;
                if (network != null) network.cancel();
            }
        }
        Attempt attempt = new Attempt();
        attempt.start(true);
        return attempt;
    }

    boolean museSyncReady() { return snapshot != null && !settingsBusy() && !catalogSyncBusy; }
    String museSyncStatus() {
        if (!museSyncStatus.isEmpty()) return museSyncStatus;
        return snapshot == null ? "Muse認証の同期にはC152とのBLE接続が必要です" : "";
    }

    void syncMuse(boolean send) {
        if (!museSyncReady()) return;
        String previous = phone.museCredential();
        final String access;
        try {
            access = send ? new org.json.JSONObject(previous).getString("token") : "";
            if (send && (!MuseChat.isAccessToken(access) || access.length() > MuseAccessSync.MAX_TOKEN))
                throw new IllegalArgumentException();
        } catch (Exception e) {
            finishMuseSync("スマホにC152用の端末アクセストークンを保存してください");
            return;
        }
        BluetoothGatt connection = gatt;
        museSyncBusy = true;
        museSyncStatus = "Muse認証を同期中…";
        museDownload = new MuseAccessSync.Download();
        changed();
        readMuseAccess(connection, MuseAccessSync.READ, () -> {
            byte[] c152 = museDownload.token();
            String candidate = send ? access : new String(c152, java.nio.charset.StandardCharsets.US_ASCII);
            if (!MuseChat.isAccessToken(candidate)) {
                finishMuseSync("C152の端末アクセストークンを確認してください");
                return;
            }
            museVerification = muse.verifyAccess(candidate, SpeechBackend.result(
                    verified -> handler.post(() -> {
                        if (gatt != connection || !museSyncBusy) return;
                        museVerification = null;
                        if (send) sendMuseAccess(connection, previous, verified);
                        else saveMuseAccess(previous, verified);
                    }),
                    error -> handler.post(() -> {
                        if (gatt != connection || !museSyncBusy) return;
                        museVerification = null;
                        finishMuseSync(error);
                    })));
        });
    }

    private void readMuseAccess(BluetoothGatt connection, int command, Runnable done) {
        writeMuseAccess(connection, MuseAccessSync.command(command, museDownload.offset()), () ->
                enqueue(new Operation(READ,
                        () -> connection.readCharacteristic(settingsCharacteristic)
                                ? BluetoothStatusCodes.SUCCESS : BluetoothStatusCodes.ERROR_UNKNOWN,
                        bytes -> {
                            try {
                                museDownload.append(bytes);
                            } catch (IllegalArgumentException e) {
                                finishMuseSync("C152の認証データを読み取れませんでした");
                                return;
                            } finally {
                                if (bytes != null) java.util.Arrays.fill(bytes, (byte) 0);
                            }
                            if (museDownload.finished())
                                writeMuseAccess(connection, MuseAccessSync.command(MuseAccessSync.CLEAR, 0), done);
                            else readMuseAccess(connection, command, done);
                        }, this::museSyncError)));
    }

    private void saveMuseAccess(String previous, String access) {
        try {
            var fields = previous.isEmpty() ? new org.json.JSONObject() : new org.json.JSONObject(previous);
            fields.put("token", access);
            if (!phone.replaceMuseCredential(previous, fields.toString())) {
                finishMuseSync("同期中にスマホの認証設定が変わったか、保存できませんでした");
                return;
            }
            finishMuseSync("Muse認証をC152から取り込みました");
        } catch (org.json.JSONException e) {
            finishMuseSync("スマホにMuse認証を保存できませんでした");
        }
    }

    private void sendMuseAccess(BluetoothGatt connection, String previous, String access) {
        if (!previous.equals(phone.museCredential())) {
            finishMuseSync("同期中にスマホの認証設定が変わりました。もう一度同期してください");
            return;
        }
        try {
            byte[] digest = MessageDigest.getInstance("SHA-256").digest(museDownload.token());
            byte[] token = access.getBytes(java.nio.charset.StandardCharsets.US_ASCII);
            museUpload = java.util.Arrays.copyOf(digest, digest.length + token.length);
            System.arraycopy(token, 0, museUpload, digest.length, token.length);
            java.util.Arrays.fill(token, (byte) 0);
            writeMuseAccess(connection, MuseAccessSync.command(MuseAccessSync.BEGIN_WRITE, token.length),
                    () -> uploadMuseAccess(connection, 0, () -> {
                        finishMuseSync("Muse認証をC152に保存しました");
                        handler.post(retryManagedPair);
                    }));
        } catch (java.security.NoSuchAlgorithmException e) {
            finishMuseSync("C152への認証送信を開始できませんでした");
        }
    }

    private void uploadMuseAccess(BluetoothGatt connection, int offset, Runnable done) {
        if (offset == museUpload.length) {
            writeMuseAccess(connection, MuseAccessSync.command(MuseAccessSync.COMMIT, 0), done);
            return;
        }
        byte[] packet = MuseAccessSync.part(mtu, offset, museUpload);
        writeMuseAccess(connection, packet, () -> uploadMuseAccess(connection, offset + packet.length - MuseAccessSync.HEADER, done));
    }

    private void syncMusePair(String rejectedAccess, Consumer<String> completion) {
        if (catalogSyncBusy) {
            BluetoothGatt connection = gatt;
            handler.postDelayed(() -> {
                if (gatt == connection) syncMusePair(rejectedAccess, completion);
                else if (completion != null) completion.accept("StopWatchとの接続が切れました");
            }, 150);
            return;
        }
        if (!museSyncReady()) {
            if (completion != null) completion.accept("Muse認証を同期中です。接続を確認してください");
            return;
        }
        BluetoothGatt connection = gatt;
        String watch = target;
        museSyncBusy = true;
        managedCompletion = completion;
        museDownload = new MuseAccessSync.Download(MuseAccessSync.MAX_PAIR);
        readMuseAccess(connection, MuseAccessSync.PAIR_READ, () -> {
            try {
                MusePair pair = MusePair.read(museDownload.token());
                handleMusePair(connection, watch, pair, rejectedAccess);
            } catch (Exception e) {
                finishMuseSync("C152のMuse認証を同期できませんでした");
            }
        });
    }

    private void handleMusePair(BluetoothGatt connection, String watch, MusePair current,
                                String rejectedAccess) throws org.json.JSONException {
        String saved = phone.museRotation(watch);
        var journal = saved.isEmpty() ? new org.json.JSONObject() : new org.json.JSONObject(saved);
        boolean matches = journal.has("revision") && journal.getLong("revision") == current.revision();
        if (current.reserved()) {
            if (!matches) {
                finishMuseSync("認証更新を開始したスマホをC152に接続してください");
                return;
            }
            switch (journal.optString("phase")) {
                case "ready" -> copyMuseRotation(connection, watch, current, journal);
                case "refused" -> releaseMuseRotation(connection, watch, current, journal);
                case "prepared" -> rotateMuse(connection, watch, current);
                default -> {
                    if (rotationsInFlight.contains(watch)) {
                        // The running worker schedules a new sync once its result is durable.
                        finishMuseSync("Museが認証を更新中です");
                    } else finishMuseSync("Museの更新結果を受信できませんでした。Museの登録を確認してください");
                }
            }
            return;
        }
        if (!saved.isEmpty()) phone.saveMuseRotation(watch, "");
        if (!activateMusePair(watch, current)) { finishMuseSync("スマホにMuse認証を保存できませんでした"); return; }
        if (rejectedAccess == null || (!rejectedAccess.isEmpty() && !rejectedAccess.equals(current.access()))) {
            finishManagedPair("Muse認証をC152と同期しました");
            return;
        }
        if (rejectedAccess.isEmpty()) {
            // The failed request used the old, manually stored copy. Try the pair from C152 first.
            finishManagedPair("Muse認証をC152と同期しました");
            return;
        }
        long now = android.os.SystemClock.elapsedRealtime();
        refreshBackoff.values().removeIf(until -> until <= now);
        if (refreshBackoff.containsKey(current.revision())) {
            finishMuseSync("Muse認証を更新できませんでした。少し待ってから再試行してください");
            return;
        }
        if (!saveRotationState(watch, current.revision(), "prepared", null)) {
            finishMuseSync("Muse認証の更新を準備できませんでした");
            return;
        }
        writeMuseAccess(connection, MuseAccessSync.reservation(MuseAccessSync.RESERVE, current.revision()),
                () -> rotateMuse(connection, watch, current));
    }

    private boolean activateMusePair(String watch, MusePair pair) throws org.json.JSONException {
        String data = new org.json.JSONObject().put("access", pair.access()).put("refresh", pair.refresh())
                .put("device", pair.device()).toString();
        if (!data.equals(phone.musePair(watch)) && !phone.saveMusePair(watch, data)) return false;
        managedPair = pair;
        return true;
    }

    private boolean saveRotationState(String watch, long revision, String phase, MuseChat.Rotation rotation) {
        try {
            var journal = new org.json.JSONObject().put("revision", revision).put("phase", phase);
            if (rotation != null) journal.put("status", rotation.status())
                    .put("access", rotation.access()).put("refresh", rotation.refresh());
            return phone.saveMuseRotation(watch, journal.toString());
        } catch (org.json.JSONException e) { return false; }
    }

    private void rotateMuse(BluetoothGatt connection, String watch, MusePair current) {
        if (!saveRotationState(watch, current.revision(), "requested", null)) {
            finishMuseSync("Muse認証の更新を開始できませんでした");
            return;
        }
        refreshBackoff.put(current.revision(), android.os.SystemClock.elapsedRealtime() + 60_000);
        rotationsInFlight.add(watch);
        NetworkTask.start(workers, SpeechBackend.<Boolean>result(
                stored -> handler.post(() -> rotationFinished(connection, watch, stored)),
                error -> handler.post(() -> rotationFinished(connection, watch, false))),
                "Museの更新結果を受信できませんでした", request -> {
                    MuseChat.Rotation result = MuseChat.rotate(request, current);
                    if (result.succeeded()) return saveRotationState(watch, current.revision(), "ready", result);
                    if (result.status() >= 400 && result.status() < 500)
                        return saveRotationState(watch, current.revision(), "refused", result);
                    return false;
                });
    }

    private void rotationFinished(BluetoothGatt connection, String watch, boolean stored) {
        rotationsInFlight.remove(watch);
        if (gatt == connection && museSyncBusy && watch.equals(target)) {
            Consumer<String> completion = managedCompletion;
            managedCompletion = null;
            clearMuseSync();
            if (stored) syncMusePair(null, completion);
            else {
                String error = "Museの更新結果を取得できませんでした。認証を確認してください";
                finishMuseSync(error);
                if (completion != null) completion.accept(error);
            }
        } else if (watch.equals(target) && stored) handler.post(retryManagedPair);
    }

    private void copyMuseRotation(BluetoothGatt connection, String watch, MusePair current,
                                  org.json.JSONObject journal) throws org.json.JSONException {
        String access = journal.getString("access"), refresh = journal.getString("refresh");
        museUpload = current.replacement(access, refresh);
        writeMuseAccess(connection, MuseAccessSync.command(MuseAccessSync.PAIR_BEGIN, museUpload.length),
                () -> uploadMuseAccess(connection, 0, () -> {
                    phone.saveMuseRotation(watch, "");
                    try {
                        var updated = new MusePair(current.revision(), false, current.device(), access, refresh);
                        if (activateMusePair(watch, updated)) finishManagedPair("スマホでMuse認証を更新し、C152に保存しました");
                        else finishMuseSync("C152に保存済み。スマホの認証をもう一度同期してください");
                    } catch (org.json.JSONException e) {
                        finishMuseSync("C152に保存済み。スマホの認証をもう一度同期してください");
                    }
                }));
    }

    private void releaseMuseRotation(BluetoothGatt connection, String watch, MusePair current,
                                     org.json.JSONObject journal) {
        int code = journal.optInt("status");
        writeMuseAccess(connection, MuseAccessSync.reservation(MuseAccessSync.RELEASE, current.revision()), () -> {
            phone.saveMuseRotation(watch, "");
            finishMuseSync("Museが端末認証の更新を拒否しました（HTTP " + code + "）");
        });
    }

    private void finishManagedPair(String message) {
        Consumer<String> completion = managedCompletion;
        managedCompletion = null;
        finishMuseSync(message);
        if (completion != null) completion.accept(null);
    }

    private void writeMuseAccess(BluetoothGatt connection, byte[] bytes, Runnable done) {
        enqueue(new Operation(WRITE, () -> connection.writeCharacteristic(settingsCharacteristic, bytes,
                BluetoothGattCharacteristic.WRITE_TYPE_DEFAULT), unused -> done.run(), this::museSyncError));
    }

    private void museSyncError(int code) {
        boolean automatic = museDownload != null && museDownload.maximum() == MuseAccessSync.MAX_PAIR;
        finishMuseSync(switch (code) {
            case 0x0d, 0x0e -> "C152のファームウェアをMuse認証同期版へ更新してください";
            case 0x80 -> "C152の「スマホから設定」でMuse認証の同期を許可してください";
            case 0x81 -> "C152の会話や設定が終わってから同期してください";
            case 0x82 -> "C152にMuseの端末アクセストークンがありません";
            case 0x83 -> "同期中にC152の認証が更新されました。もう一度同期してください";
            case 0x84 -> "C152にMuse認証を保存できませんでした";
            default -> "Muse認証を同期できませんでした。C152で同期の許可と接続を確認してください";
        });
        if (automatic && (code == 0x80 || code == 0x81) && speechReady)
            handler.postDelayed(retryManagedPair, code == 0x80 ? 6_000 : 1_500);
    }

    private void clearMuseSync() {
        if (museVerification != null) museVerification.cancel();
        if (museDownload != null) museDownload.clear();
        if (museUpload != null) java.util.Arrays.fill(museUpload, (byte) 0);
        museVerification = null;
        museDownload = null;
        museUpload = null;
        museSyncBusy = false;
    }

    private void finishMuseSync(String message) {
        Consumer<String> completion = managedCompletion;
        managedCompletion = null;
        clearMuseSync();
        museSyncStatus = message;
        log.event(ConversationLog.MUSE, message);
        changed();
        if (completion != null) completion.accept(message);
    }

    private void pollDeviceMode() {
        BluetoothGatt connection = gatt;
        if (connection == null || snapshot == null || speechReady) return;
        if (!settingsBusy() && !catalogSyncBusy) readSettings(value -> {
            if (value.values()[0] == 1) {
                snapshotChanged(value);
                setupSpeech(connection);
            }
        });
        handler.postDelayed(pollMode, 2_000);
    }

    private void changed() {
        if (listener != null) listener.run();
        scheduleReplyWatch();
    }

    private void scheduleReplyWatch() {
        handler.removeCallbacks(updateReplyWatch);
        handler.post(updateReplyWatch);
    }

    private String replyWatchSession() {
        if (!speechReady || !audio.supportsPush() || watchChats == null || audio.recordingTurn() || firmware != null ||
                catalogRefreshing() || phone.echo() ||
                (!museManaged() && !phone.hasCredential("muse"))) return null;
        return selectedChatId();
    }

    private void stopReplyWatch() {
        handler.removeCallbacks(updateReplyWatch);
        ++watchGeneration;
        if (replyWatch != null) replyWatch.cancel();
        replyWatch = null;
        watchedSession = null;
        watchRetryAt = 0;
        watchRetryDelay = 2_000;
        externalReplies.clear();
    }

    private void updateReplyWatch() {
        String session = replyWatchSession();
        if (!java.util.Objects.equals(session, watchedSession)) {
            stopReplyWatch();
            watchedSession = session;
        }
        if (session == null) return;
        playNextReply();
        if (replyWatch != null) return;
        long wait = watchRetryAt - SystemClock.elapsedRealtime();
        if (wait > 0) {
            handler.postDelayed(updateReplyWatch, wait);
            return;
        }
        long generation = ++watchGeneration;
        String access = managedPair == null ? "" : managedPair.access();
        replyWatch = muse.watch(session, () -> handler.post(() -> {
            if (generation != watchGeneration) return;
            watchRetryDelay = 2_000;
            String detail = "Museの外部返信を待受中 · " + (session.isEmpty() ? "メインチャット" : "選択中のサイドチャット");
            log.event(ConversationLog.MUSE, detail);
            Log.i("SumiMuse", detail);
        }), (id, text) -> handler.post(() -> {
            if (generation != watchGeneration || !session.equals(replyWatchSession()) || !deliveredReplies.add(id)) return;
            if (deliveredReplies.size() > 256) deliveredReplies.remove(deliveredReplies.iterator().next());
            if (externalReplies.size() >= 4) {
                log.event(ConversationLog.MUSE, "外部返信の再生待ちが満杯です");
                return;
            }
            log.body(ConversationLog.MUSE, "受信 · Museアプリなどからの返信", text);
            Log.i("SumiMuse", "External reply received (" + text.length() + " characters)");
            externalReplies.addLast(text);
            playNextReply();
        }), reason -> handler.post(() -> {
            if (generation != watchGeneration) return;
            replyWatch = null;
            log.event(ConversationLog.MUSE, "外部返信の待受 · " + reason);
            Log.i("SumiMuse", "External reply subscription stopped: " + reason);
            watchRetryAt = SystemClock.elapsedRealtime() + watchRetryDelay;
            watchRetryDelay = Math.min(30_000, 2 * watchRetryDelay);
            if (MuseChat.accessRejected(reason) && managedPair != null && !settingsBusy())
                syncMusePair(access, error -> {
                    if (generation != watchGeneration || error != null) return;
                    watchRetryAt = 0;
                    scheduleReplyWatch();
                });
            scheduleReplyWatch();
        }));
    }

    private void playNextReply() {
        if (audio.busy() || settingsBusy() || catalogSyncBusy || externalReplies.isEmpty()) return;
        String text = externalReplies.removeFirst();
        audio.push(watchChats, output -> pipeline.playExternal(text, output));
    }

    private Notification notification() {
        var launch = PendingIntent.getActivity(this, 0, new Intent(this, MainActivity.class),
                PendingIntent.FLAG_IMMUTABLE | PendingIntent.FLAG_UPDATE_CURRENT);
        return new Notification.Builder(this, CHANNEL).setSmallIcon(android.R.drawable.stat_sys_data_bluetooth)
                .setContentTitle("すみ StopWatch").setContentText(link + " · " + name())
                .setContentIntent(launch).setOnlyAlertOnce(true).setOngoing(true).build();
    }

    private void update(String detail, String connection) {
        boolean connectionChanged = !link.equals(connection);
        status = detail;
        link = connection;
        log.event(ConversationLog.BLE, detail);
        if (connectionChanged && foreground)
            getSystemService(NotificationManager.class).notify(NOTIFICATION_ID, notification());
        changed();
    }

    private void stage(String message) {
        status = message;
        changed();
    }

    private void assetMessage(String message) {
        assets = message;
        changed();
    }

    void pauseConnection() {
        wanted = false;
        closeLink();
        phone.autoConnect(false);
        update("自動接続はオフです", "停止中");
        stopForeground(STOP_FOREGROUND_REMOVE);
        foreground = false;
        stopSelf();
    }

    private void snapshotChanged(DeviceSettings.Snapshot value) {
        snapshot = value;
        if (BluetoothAdapter.checkBluetoothAddress(target) && !target.equals(phone.deviceAddress()))
            phone.saveDevice(target, name);
        reconnectDelay = 1_000;
        changed();
    }

    void refreshSettings() {
        if (!settingsBusy() && !catalogSyncBusy) readSettings(this::snapshotChanged);
    }

    void updateSetting(int key, int value) {
        if (gatt == null || snapshot == null || settingsBusy()) return;
        if (catalogSyncBusy) {
            BluetoothGatt connection = gatt;
            handler.postDelayed(() -> { if (gatt == connection) updateSetting(key, value); }, 120);
            return;
        }
        settingPending = true;
        changed();
        settingsSequence = settingsSequence % 65535 + 1;
        int sequence = settingsSequence;
        byte[] packet = DeviceSettings.command(sequence, key, value);
        BluetoothGatt connection = gatt;
        enqueue(new Operation(WRITE, () -> connection.writeCharacteristic(settingsCharacteristic, packet,
                BluetoothGattCharacteristic.WRITE_TYPE_DEFAULT), unused -> waitSetting(connection, sequence, 30)));
    }

    private void waitSetting(BluetoothGatt connection, int sequence, int attempts) {
        if (connection != gatt) return;
        readSettings(value -> {
            if (value.sequence() != sequence && attempts > 0) {
                handler.postDelayed(() -> waitSetting(connection, sequence, attempts - 1), 60);
                return;
            }
            settingPending = false;
            snapshotChanged(value);
            scheduleChatCatalog(120);
            if (value.sequence() != sequence) { stage("設定の保存を確認できませんでした"); return; }
            if (value.result() != 0) {
                stage(value.result() == 2 ? "会話や設定操作の完了後に変更してください" : "C152に設定を保存できませんでした");
                return;
            }
            log.event(ConversationLog.BLE, "C152の設定を保存しました");
            if (value.values()[0] == 1) {
                if (speechReady) stage("C152の設定を保存しました");
                else setupSpeech(connection);
            } else {
                speechReady = false;
                audio.reset();
                audio.features(0);
                update("Wi-Fiモード · 本体設定を操作できます", "設定に接続");
                handler.removeCallbacks(pollMode);
                handler.postDelayed(pollMode, 2_000);
            }
        });
    }

    void startScan() {
        if (!hasBluetooth(this) || adapter == null || !adapter.isEnabled()) return;
        stopScan();
        nearby.clear();
        var scanner = adapter.getBluetoothLeScanner();
        if (scanner == null) { stage("Bluetoothの検索を開始できません"); return; }
        scan = new ScanCallback() {
            @Override public void onScanFailed(int code) {
                handler.post(() -> {
                    if (scan == this) { stopScan(); stage("検索に失敗しました（" + code + "）"); }
                });
            }
            @Override public void onScanResult(int type, ScanResult result) {
                handler.post(() -> {
                    if (scan != this || result.getScanRecord() == null) return;
                    String label = result.getScanRecord().getDeviceName();
                    if (label == null || !label.startsWith("MuseGadget")) return;
                    String address = result.getDevice().getAddress();
                    if (nearby.putIfAbsent(address, new Nearby(address, label)) == null) changed();
                });
            }
        };
        scanner.startScan(List.of(new ScanFilter.Builder().setServiceUuid(new ParcelUuid(ADVERTISEMENT)).build()),
                new ScanSettings.Builder().setScanMode(ScanSettings.SCAN_MODE_LOW_LATENCY).build(), scan);
        stage("近くのStopWatchを検索中");
        ScanCallback started = scan;
        handler.postDelayed(() -> {
            if (scan != started) return;
            stopScan();
            stage(nearby.isEmpty() ? "見つかりません。C152のBLEモードを確認してください" : "接続するStopWatchを選択");
        }, 12_000);
    }

    private void stopScan() {
        if (scan == null) return;
        if (hasBluetooth(this) && adapter != null) {
            var scanner = adapter.getBluetoothLeScanner();
            if (scanner != null) scanner.stopScan(scan);
        }
        scan = null;
    }

    private void open(boolean direct) {
        if (!wanted || gatt != null) return;
        if (!hasBluetooth(this) || adapter == null || !adapter.isEnabled()) {
            update("Bluetoothをオンにすると再接続します", "Bluetoothオフ");
            return;
        }
        handler.removeCallbacks(reconnect);
        update(direct ? "登録したStopWatchに接続中" : "圏内に入ると自動で再接続", direct ? "接続中" : "再接続待ち");
        try {
            var device = adapter.getRemoteDevice(target);
            gatt = device.connectGatt(this, !direct, callbacks, BluetoothDevice.TRANSPORT_LE);
        } catch (IllegalArgumentException | SecurityException e) {
            disconnect("StopWatchへの接続を開始できません");
            return;
        }
        if (gatt == null) { disconnect("StopWatchへの接続を開始できません"); return; }
        BluetoothGatt connection = gatt;
        if (direct) handler.postDelayed(() -> {
            if (gatt == connection && rx == null) disconnect("StopWatchが応答しません");
        }, 12_000);
    }

    private void closeLink() {
        stopReplyWatch();
        handler.removeCallbacks(probeFirmware);
        firmwareInfo = null;
        firmwareProbeBusy = firmwareUnsupported = false;
        if (firmware != null) {
            if (firmware.rebooting()) firmware.disconnected = true;
            else finishFirmware("BLEが切断され、ファームウェア更新を中断しました");
        }
        stopScan();
        handler.removeCallbacks(reconnect);
        handler.removeCallbacks(operationTimeout);
        handler.removeCallbacks(retryManagedPair);
        handler.removeCallbacks(pollMode);
        handler.removeCallbacks(syncCatalog);
        operations.clear();
        running = null;
        rx = tx = settingsCharacteristic = null;
        if (museSyncBusy) museSyncStatus = "接続が切れたためMuse認証の同期を中断しました";
        clearMuseSync();
        catalogSyncBusy = catalogUnsupported = false;
        watchChats = null;
        catalogLinkProblem = "";
        managedPair = null;
        Consumer<String> completion = managedCompletion;
        managedCompletion = null;
        speechReady = settingPending = false;
        snapshot = null;
        if (gatt != null) { gatt.close(); gatt = null; }
        if (audio != null) { audio.reset(); audio.features(0); }
        if (wake.isHeld()) wake.release();
        if (completion != null) completion.accept(museSyncStatus);
    }

    private void disconnect(String message) {
        closeLink();
        String saved = phone.deviceAddress();
        if (saved.isEmpty() || !wanted) {
            update(message, "未接続");
            foreground = wanted = false;
            stopForeground(STOP_FOREGROUND_REMOVE);
            stopSelf();
            return;
        }
        target = saved;
        name = phone.deviceName();
        update(message + "。圏内に入ると再接続", "再接続待ち");
        handler.postDelayed(reconnect, reconnectDelay);
        reconnectDelay = Math.min(20_000, reconnectDelay * 2);
    }

    // Connection management
    private final BluetoothGattCallback callbacks = new BluetoothGattCallback() {
        @Override public void onConnectionStateChange(BluetoothGatt connection, int code, int state) {
            handler.post(() -> {
                if (gatt != connection) return;
                if (code != BluetoothGatt.GATT_SUCCESS || state != BluetoothProfile.STATE_CONNECTED) {
                    disconnect("StopWatchとの接続が切れました（" + code + "）");
                    return;
                }
                connection.requestConnectionPriority(BluetoothGatt.CONNECTION_PRIORITY_HIGH);
                handler.postDelayed(() -> {
                    if (connection == gatt && rx == null) disconnect("StopWatchのサービスを読み取れません");
                }, 15_000);
                audio.mtu(23);
                mtu = 23;
                if (!connection.requestMtu(C152Ota.MTU)) discover(connection);
            });
        }
        @Override public void onMtuChanged(BluetoothGatt connection, int mtu, int code) {
            handler.post(() -> {
                if (gatt != connection) return;
                if (code == BluetoothGatt.GATT_SUCCESS) {
                    ConversationService.this.mtu = mtu;
                    audio.mtu(mtu);
                }
                discover(connection);
            });
        }
        @Override public void onServicesDiscovered(BluetoothGatt connection, int code) {
            handler.post(() -> {
                if (gatt != connection) return;
                BluetoothGattService service = connection.getService(SERVICE);
                if (code != BluetoothGatt.GATT_SUCCESS || service == null) {
                    disconnect("StopWatchの音声サービスが見つかりません"); return;
                }
                rx = service.getCharacteristic(RX);
                tx = service.getCharacteristic(TX);
                settingsCharacteristic = service.getCharacteristic(SETTINGS);
                if (rx == null || tx == null || settingsCharacteristic == null) {
                    disconnect("会話版のC152ファームウェアへ更新してください"); return;
                }
                if (connection.getDevice().getBondState() != BluetoothDevice.BOND_BONDED)
                    update("初回接続は、C152に表示された6桁のコードを入力", "ペアリング");
                readSettings(snapshot -> {
                    snapshotChanged(snapshot);
                    if (snapshot.values()[0] == 1) setupSpeech(connection);
                    else {
                        update("本体設定を操作できます", "設定に接続");
                        syncMusePair(null, null);
                        handler.postDelayed(pollMode, 2_000);
                    }
                    scheduleChatCatalog(1_000);
                });
            });
        }
        @Override public void onDescriptorWrite(BluetoothGatt connection, BluetoothGattDescriptor descriptor, int code) {
            handler.post(() -> complete(connection, DESCRIPTOR, code, null));
        }
        @Override public void onCharacteristicWrite(BluetoothGatt connection, BluetoothGattCharacteristic characteristic, int code) {
            handler.post(() -> complete(connection, WRITE, code, null));
        }
        @Override public void onCharacteristicRead(BluetoothGatt connection, BluetoothGattCharacteristic characteristic, byte[] value, int code) {
            handler.post(() -> complete(connection, READ, code, value));
        }
        @Override public void onCharacteristicChanged(BluetoothGatt connection, BluetoothGattCharacteristic characteristic, byte[] value) {
            handler.post(() -> { if (gatt == connection && characteristic.getUuid().equals(TX)) audio.onNotification(value); });
        }
    };

    private void setupSpeech(BluetoothGatt connection) {
        if (speechReady || gatt != connection) return;
        BluetoothGattDescriptor cccd = tx.getDescriptor(CCCD);
        if (cccd == null || !connection.setCharacteristicNotification(tx, true)) {
            disconnect("音声の受信設定ができません"); return;
        }
        enqueue(new Operation(DESCRIPTOR,
                () -> connection.writeDescriptor(cccd, BluetoothGattDescriptor.ENABLE_NOTIFICATION_VALUE),
                unused -> write(new AudioPacket(AudioPacket.HELLO, 0, AudioPacket.VERSION), true, () -> {
                    speechReady = true;
                    handler.removeCallbacks(pollMode);
                    readStatus(packet -> {
                        if (packet.type() == AudioPacket.REPLY_STATUS && packet.turn() == 0) {
                            audio.features(packet.offset());
                            if ((packet.offset() & AudioPacket.FEATURE_NOTE_STREAM) != 0)
                                write(new AudioPacket(AudioPacket.NOTE_STREAM_ENABLE, 0, 0), true, null);
                        }
                        if (!audio.supportsPush())
                            log.event(ConversationLog.BLE, "Museの外部返信を再生するにはC152の更新が必要です");
                        reconnectDelay = 1_000;
                        update("C152の黄ボタンで会話", "会話できます");
                        syncMusePair(null, null);
                        handler.postDelayed(probeFirmware, 1_000);
                    });
                })));
    }

    private void readSettings(Consumer<DeviceSettings.Snapshot> receive) {
        if (gatt == null || settingsCharacteristic == null) return;
        enqueue(new Operation(READ,
                () -> gatt.readCharacteristic(settingsCharacteristic) ? BluetoothStatusCodes.SUCCESS : BluetoothStatusCodes.ERROR_UNKNOWN,
                bytes -> {
                    try { receive.accept(DeviceSettings.Snapshot.read(bytes)); }
                    catch (IllegalArgumentException e) { disconnect("C152設定の形式が一致しません"); }
                }));
    }

    // OTA shares the authenticated settings characteristic, with its own command namespace.
    boolean updatingFirmware() { return firmware != null || readingFirmware; }

    boolean canUpdateFirmware() {
        return speechReady && firmwareInfo != null && firmwareInfo.confirmed() && !settingsBusy() &&
                !catalogSyncBusy && !catalogRefreshing() && !audio.busy() && !readingFirmware;
    }

    String firmwareStatus() {
        if (!firmwareMessage.isEmpty()) return firmwareMessage;
        if (firmwareUnsupported) return "OTA対応ファームの初回導入はUSBで行います";
        if (firmwareInfo != null && !firmwareInfo.confirmed()) return "C152の起動確認中…";
        return "";
    }

    private void probeFirmware() {
        if (!speechReady || gatt == null || firmwareUnsupported || firmwareProbeBusy ||
                (firmware != null && !firmware.rebooting())) return;
        BluetoothGatt connection = gatt;
        if (settingPending || museSyncBusy || catalogSyncBusy || audio.busy() ||
                (firmware != null && SystemClock.elapsedRealtime() < firmware.verifyAfter)) {
            handler.postDelayed(probeFirmware, 1_000);
            return;
        }
        firmwareProbeBusy = true;
        IntConsumer failed = code -> {
            if (connection != gatt) return;
            firmwareProbeBusy = false;
            if (firmware == null && (code == 0x06 || code == 0x0d || code == 0x0e)) firmwareUnsupported = true;
            else handler.postDelayed(probeFirmware, 3_000);
            changed();
        };
        Runnable read = () -> enqueue(new Operation(READ,
                () -> connection.readCharacteristic(settingsCharacteristic) ? BluetoothStatusCodes.SUCCESS : BluetoothStatusCodes.ERROR_UNKNOWN,
                value -> {
                    firmwareProbeBusy = false;
                    try { firmwareInfo = C152Ota.Info.read(value); }
                    catch (IllegalArgumentException e) { failed.accept(0x0d); return; }
                    if (firmware != null && firmwareInfo.error() != 0) {
                        firmwareWriteFailed(firmware, firmwareInfo.error());
                    } else if (firmware != null && firmware.disconnected && firmwareInfo.confirmed()) {
                        Log.i("SumiOta", "Boot readback: old slot=" + firmware.oldSlot + ", running slot=" + firmwareInfo.slot());
                        if (firmwareInfo.matches(firmware.image, firmware.oldSlot))
                            finishFirmware("C152の更新と再起動を確認しました");
                        else finishFirmware("C152は更新前のファームウェアで起動しました");
                    } else if (firmware != null || !firmwareInfo.confirmed()) handler.postDelayed(probeFirmware, 2_000);
                    changed();
                }, failed));
        writeFirmware(C152Ota.command(C152Ota.INFO, C152Ota.INFO_EXTENDED), read, code -> {
            if (connection != gatt) return;
            if (code == 0x06 || code == 0x0d || code == 0x0e)
                writeFirmware(C152Ota.command(C152Ota.INFO, 0), read, failed);
            else failed.accept(code);
        });
    }

    void importFirmware(Uri uri) {
        if (!canUpdateFirmware()) return;
        BluetoothGatt connection = gatt;
        boolean compression = firmwareInfo.compressed();
        readingFirmware = true;
        firmwareMessage = "ファームウェアを読み込み中…";
        changed();
        workers.execute(() -> {
            C152Ota.Image loaded = null;
            C152Ota.Upload prepared = null;
            try (var stream = getContentResolver().openInputStream(uri)) {
                if (stream != null) {
                    loaded = C152Ota.Image.read(OpenAiAsr.readBounded(stream, C152Ota.MAX_BYTES));
                    prepared = C152Ota.Upload.prepare(loaded, compression);
                }
            } catch (IOException | RuntimeException e) {
                Log.i("SumiOta", "Firmware import failed: " + e.getClass().getSimpleName());
            }
            C152Ota.Image image = loaded;
            C152Ota.Upload upload = prepared;
            handler.post(() -> {
                readingFirmware = false;
                if (image == null || upload == null) {
                    firmwareMessage = "対応する署名済みファームウェアを開けませんでした";
                    changed();
                } else if (connection != gatt || !canUpdateFirmware()) {
                    firmwareMessage = "C152の接続と待機状態を確認してからやり直してください";
                    changed();
                } else {
                    var transfer = new FirmwareTransfer(image, upload, firmwareInfo.slot());
                    firmware = transfer;
                    wake.acquire(30 * 60_000L);
                    stopReplyWatch();
                    connection.requestConnectionPriority(BluetoothGatt.CONNECTION_PRIORITY_HIGH);
                    firmwareMessage = "C152で更新準備中…";
                    changed();
                    beginFirmware(transfer);
                }
            });
        });
    }

    private void beginFirmware(FirmwareTransfer transfer) {
        C152Ota.Upload upload = transfer.upload;
        int rawLength = transfer.image.data().length;
        Log.i("SumiOta", "Transfer starting: raw=" + rawLength + " B, wire=" +
                upload.bytes().length + " B, ATT MTU=" + mtu + ", compressed=" + upload.compressed());
        int begin = upload.compressed() ? C152Ota.BEGIN_COMPRESSED : C152Ota.BEGIN;
        writeFirmware(C152Ota.command(begin, rawLength), () -> sendFirmwarePart(transfer), code -> {
            if (transfer == firmware && code == C152Ota.NO_MEMORY && upload.compressed()) {
                Log.i("SumiOta", "Compressed OTA buffers unavailable; switching to the raw image");
                firmware = new FirmwareTransfer(transfer.image,
                        C152Ota.Upload.prepare(transfer.image, false), transfer.oldSlot);
                beginFirmware(firmware);
            } else firmwareWriteFailed(transfer, code);
        });
    }

    private void sendFirmwarePart(FirmwareTransfer transfer) {
        if (transfer != firmware) return;
        int length = transfer.upload.bytes().length;
        int progress = (int)((long)transfer.offset * 100 / length);
        long elapsed = SystemClock.elapsedRealtime() - transfer.started;
        if (transfer.percent != progress) {
            transfer.percent = progress;
            firmwareMessage = "C152へファームウェアを転送中 · " + progress + "%";
            if (progress >= 5 && progress < 100 && elapsed >= 2_000) {
                long remaining = elapsed * (length - transfer.offset) / transfer.offset;
                firmwareMessage += " · 残り約 " + android.text.format.DateUtils.formatElapsedTime((remaining + 999) / 1000);
            }
            changed();
        }
        if (transfer.offset == length) {
            Log.i("SumiOta", "Transfer complete: " + length + " B in " + elapsed + " ms");
            transfer.verifyAfter = SystemClock.elapsedRealtime() + 3_000;
            firmwareMessage = "署名の検証とC152の再起動を待機中…";
            changed();
            handler.postDelayed(firmwareBootTimeout, 330_000);
            handler.removeCallbacks(probeFirmware);
            handler.postDelayed(probeFirmware, 3_000);
            writeFirmware(C152Ota.command(C152Ota.FINISH, transfer.image.data().length),
                    () -> {}, code -> firmwareWriteFailed(transfer, code));
            return;
        }
        byte[] part = transfer.upload.part(transfer.offset, mtu);
        writeFirmware(part, () -> {
            transfer.offset += part.length - C152Ota.HEADER;
            sendFirmwarePart(transfer);
        }, code -> firmwareWriteFailed(transfer, code));
    }

    private void firmwareWriteFailed(FirmwareTransfer transfer, int code) {
        if (firmware != transfer) return;
        // FINISH can be committed just before BLE drops. Reconnect and read back the slot in that case.
        if (transfer.rebooting() && (code == 133 || code == BluetoothStatusCodes.ERROR_UNKNOWN)) return;
        if (!transfer.rebooting() && gatt != null)
            writeFirmware(C152Ota.command(C152Ota.ABORT, 0), () -> {}, ignored -> {});
        finishFirmware(switch (code) {
            case 0x80 -> "C152の会話や設定が終わってから更新してください";
            case 0x82 -> "C152がファームウェアの署名またはイメージを拒否しました";
            default -> "C152のファームウェア更新に失敗しました（BLE " + code + "）";
        });
    }

    private void finishFirmware(String message) {
        firmware = null;
        handler.removeCallbacks(firmwareBootTimeout);
        if (wake.isHeld() && !audio.busy()) wake.release();
        if (gatt != null) gatt.requestConnectionPriority(BluetoothGatt.CONNECTION_PRIORITY_BALANCED);
        firmwareMessage = message;
        Log.i("SumiOta", message);
        log.event(ConversationLog.BLE, message);
        handler.post(() -> { if (gatt != null && speechReady) syncMusePair(null, null); });
        changed();
    }

    private void writeFirmware(byte[] bytes, Runnable done, IntConsumer failed) {
        BluetoothGatt connection = gatt;
        BluetoothGattCharacteristic settings = settingsCharacteristic;
        if (connection == null || settings == null) { failed.accept(BluetoothStatusCodes.ERROR_UNKNOWN); return; }
        enqueue(new Operation(WRITE, () -> connection.writeCharacteristic(settings, bytes,
                BluetoothGattCharacteristic.WRITE_TYPE_DEFAULT), unused -> done.run(), failed));
    }


    // Model and credential imports
    void importMuse(Uri uri) {
        String previous = phone.museCredential();
        workers.submit(() -> {
            try (var input = getContentResolver().openInputStream(uri)) {
                if (input == null) throw new IOException("Cannot read Muse registration");
                byte[] bytes = OpenAiAsr.readBounded(input, 40 * 1024);
                var fields = new org.json.JSONObject(new String(bytes, java.nio.charset.StandardCharsets.UTF_8));
                String token = fields.getString("token");
                String vm = fields.optString("vm");
                if (!MuseChat.isAccessToken(token) || !vm.matches("[A-Za-z0-9_-]{0,128}"))
                    throw new IOException("Invalid Muse device access credential");
                String credential = new org.json.JSONObject().put("token", token).put("vm", vm).toString();
                if (!phone.replaceMuseCredential(previous, credential))
                    throw new IOException("Cannot save Muse registration");
                handler.post(() -> assetMessage("Muse端末のアクセストークンを取り込みました"));
            } catch (Exception e) {
                handler.post(() -> assetMessage("Muse端末のアクセストークンを取り込めませんでした"));
            }
        });
    }

    private String displayName(Uri uri) {
        try (var cursor = getContentResolver().query(uri, new String[]{OpenableColumns.DISPLAY_NAME}, null, null, null)) {
            if (cursor != null && cursor.moveToFirst() && cursor.getString(0) != null) return cursor.getString(0);
        }
        return "";
    }

    void importModel(List<Uri> selected) {
        assetMessage("Irodoriモデルを取り込み中…");
        inference.submit(() -> {
            File temp = new File(getFilesDir(), IrodoriTts.MODEL + ".tmp");
            try {
                var parts = new ArrayList<IrodoriModels.Part<Uri>>();
                for (Uri uri : selected) parts.add(new IrodoriModels.Part<>(displayName(uri), uri));
                var selection = IrodoriModels.select(parts);
                var expected = selection.model();
                long maximum = expected != null ? expected.bytes() : IrodoriModels.maximumSize();
                MessageDigest sha = MessageDigest.getInstance("SHA-256");
                long total = 0;
                byte[] bytes = new byte[1024 * 1024];
                try (var output = Files.newOutputStream(temp.toPath())) {
                    for (var part : selection.parts()) {
                        try (var input = getContentResolver().openInputStream(part.source())) {
                            if (input == null) throw new NetworkTask.Failure("モデルファイルを開けませんでした");
                            for (int count; (count = input.read(bytes)) != -1;) {
                                total += count;
                                if (total > maximum) throw new NetworkTask.Failure("モデルのサイズが一致しません");
                                sha.update(bytes, 0, count);
                                output.write(bytes, 0, count);
                            }
                        }
                    }
                }
                var model = IrodoriModels.bySize(total);
                if (model == null || (expected != null && !expected.equals(model)) || !model.sha256().equals(hex(sha.digest())))
                    throw new NetworkTask.Failure("モデルが一致しません。分割の場合は part01 から最後まで選んでください");
                Files.move(temp.toPath(), new File(getFilesDir(), IrodoriTts.MODEL).toPath(),
                        StandardCopyOption.REPLACE_EXISTING, StandardCopyOption.ATOMIC_MOVE);
                irodori.reload();
                if (!phone.echo()) irodori.warmUp();
                handler.post(() -> assetMessage("Irodoriモデルを取り込みました\n" + model.name()));
            } catch (NetworkTask.Failure e) {
                handler.post(() -> assetMessage(e.getMessage()));
            } catch (Exception e) {
                handler.post(() -> assetMessage("Irodoriモデルを取り込めませんでした"));
            } finally {
                temp.delete();
            }
        });
    }

    void importVoice(Uri uri) {
        assetMessage("すみの参照を取り込み中…");
        inference.submit(() -> {
            File temp = new File(getFilesDir(), IrodoriTts.VOICE + ".tmp");
            File converted = new File(getFilesDir(), IrodoriTts.VOICE + ".converted");
            try {
                try (var input = getContentResolver().openInputStream(uri)) {
                    if (input == null) throw new IOException("Cannot open reference");
                    Files.write(temp.toPath(), OpenAiAsr.readBounded(input, 32 << 20));
                }
                File ready = temp;
                try (var input = Files.newInputStream(temp.toPath())) {
                    if (java.util.Arrays.equals(input.readNBytes(8),
                            "SUMIRF01".getBytes(java.nio.charset.StandardCharsets.US_ASCII))) {
                        IrodoriTts.convertReference(new File(getFilesDir(), IrodoriTts.MODEL), temp, converted);
                        ready = converted;
                    }
                }
                String target = IrodoriTts.voiceFile(ready);
                if (target == null) throw new NetworkTask.Failure("すみの参照ファイルを選んでください");
                Files.move(ready.toPath(), new File(getFilesDir(), target).toPath(),
                        StandardCopyOption.REPLACE_EXISTING, StandardCopyOption.ATOMIC_MOVE);
                irodori.reload();
                if (!phone.echo()) irodori.warmUp();
                handler.post(() -> assetMessage("すみの参照を取り込みました"));
            } catch (NetworkTask.Failure e) {
                handler.post(() -> assetMessage(e.getMessage()));
            } catch (Exception | LinkageError e) {
                handler.post(() -> assetMessage("すみの参照を取り込めませんでした"));
            } finally {
                temp.delete();
                converted.delete();
            }
        });
    }

    private static String hex(byte[] bytes) {
        StringBuilder out = new StringBuilder(bytes.length * 2);
        for (byte b : bytes) out.append(Character.forDigit((b & 255) >> 4, 16)).append(Character.forDigit(b & 15, 16));
        return out.toString();
    }

    private void discover(BluetoothGatt connection) {
        if (rx == null && !connection.discoverServices()) disconnect("BLEサービスを取得できません");
    }

    @Override public void write(AudioPacket packet, boolean acknowledged, Runnable next) {
        if (gatt == null || rx == null) return;
        byte[] bytes = packet.encode();
        int type = acknowledged ? BluetoothGattCharacteristic.WRITE_TYPE_DEFAULT : BluetoothGattCharacteristic.WRITE_TYPE_NO_RESPONSE;
        enqueue(new Operation(WRITE, () -> gatt.writeCharacteristic(rx, bytes, type), unused -> {
            if (next != null) next.run();
        }));
    }

    @Override public void readStatus(Consumer<AudioPacket> next) {
        if (gatt == null || tx == null) return;
        enqueue(new Operation(READ,
                () -> gatt.readCharacteristic(tx) ? BluetoothStatusCodes.SUCCESS : BluetoothStatusCodes.ERROR_UNKNOWN,
                bytes -> {
                    try { next.accept(AudioPacket.decode(bytes)); }
                    catch (IllegalArgumentException e) { disconnect("StopWatchの応答形式が不正です"); }
                }));
    }

    @Override public void offerPush(AudioPacket packet, Runnable rejected) {
        BluetoothGatt connection = gatt;
        BluetoothGattCharacteristic characteristic = rx;
        if (connection == null || characteristic == null) { rejected.run(); return; }
        byte[] bytes = packet.encode();
        enqueue(new Operation(WRITE, () -> connection.writeCharacteristic(characteristic, bytes,
                BluetoothGattCharacteristic.WRITE_TYPE_DEFAULT), unused -> {}, code -> rejected.run()));
    }

    @Override public void after(Runnable next, long millis) { handler.postDelayed(next, millis); }

    private void enqueue(Operation operation) {
        operations.addLast(operation);
        startNext();
    }

    private void startNext() {
        if (gatt == null || running != null || operations.isEmpty()) return;
        Operation operation = operations.removeFirst();
        running = operation;
        if (operation.start().getAsInt() != BluetoothStatusCodes.SUCCESS) {
            if (operation.failure() == null) disconnect("StopWatchへの通信を開始できません");
            else {
                running = null;
                operation.failure().accept(BluetoothStatusCodes.ERROR_UNKNOWN);
                startNext();
            }
            return;
        }
        handler.postDelayed(operationTimeout, 25000);
    }

    private void complete(BluetoothGatt connection, int type, int code, byte[] value) {
        if (connection != gatt || running == null || running.type() != type) return;
        handler.removeCallbacks(operationTimeout);
        Operation operation = running;
        running = null;
        if (code == BluetoothGatt.GATT_SUCCESS) operation.complete().accept(value);
        else if (operation.failure() != null) operation.failure().accept(code);
        else { disconnect("StopWatchとの通信でエラーが発生しました（" + code + "）"); return; }
        startNext();
    }

}
