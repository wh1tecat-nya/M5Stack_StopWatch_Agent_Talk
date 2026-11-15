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
import android.provider.OpenableColumns;

import java.io.File;
import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.StandardCopyOption;
import java.security.MessageDigest;
import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.function.Consumer;
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
    private static final String CHANNEL = "stopwatch_connection";
    private static final String CONNECT = "jp.sumi.stopwatch.CONNECT";
    static final String ADDRESS = "address", NAME = "name";
    private record Operation(int type, IntSupplier start, Consumer<byte[]> complete) {}
    record Nearby(String address, String name) {}
    private final ConversationLog log = ConversationLog.INSTANCE;
    private final Map<String, Nearby> nearby = new LinkedHashMap<>();
    private final Binder binder = new LocalBinder();
    private Runnable listener;
    private String target = "", name = "", link = "未接続";
    private String status = "StopWatchを登録すると自動で接続します";
    private String assets = "";
    private DeviceSettings.Snapshot snapshot;
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
    private final ExecutorService workers = Executors.newCachedThreadPool();
    private final ExecutorService inference = Executors.newSingleThreadExecutor();
    private PhoneSettings phone;
    private IrodoriTts irodori;
    private MuseChat muse;
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
        log.includeText(phone.logText());
        irodori = new IrodoriTts(this, inference);
        var openai = new OpenAiAsr(phone::openAiKey, workers);
        var android = new AndroidAsr(this, handler, workers);
        muse = new MuseChat(phone::museCredential, workers);
        asrBackends.put("openai", new SpeechBackend.Provider<>("OpenAI gpt-transcribe", openai));
        asrBackends.put("android", new SpeechBackend.Provider<>("Android 端末内", android));
        ttsBackends.put("irodori", new SpeechBackend.Provider<>("Irodori-TTS", irodori));
        pipeline = new SpeechPipeline(handler,
                () -> new SpeechBackend.Providers(asrBackends.getOrDefault(phone.asr(), asrBackends.get("openai")).backend(),
                        muse, ttsBackends.getOrDefault(phone.tts(), ttsBackends.get("irodori")).backend()),
                this::stage, workers);
        audio = new StopwatchAudio(this, new StopwatchAudio.Speech() {
            @Override public void start(StopwatchAudio.Recording note, StopwatchAudio.Reply reply) {
                if (phone.echo()) reply.play(note.pcm());
                else pipeline.start(note, reply);
            }
            @Override public void cancel() { pipeline.cancel(); }
        }, this::stage, active -> {
            if (active) wake.acquire(210_000);
            else if (wake.isHeld()) wake.release();
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
    boolean settingsBusy() { return settingPending; }
    DeviceSettings.Snapshot settings() { return snapshot; }
    List<Nearby> nearby() { return List.copyOf(nearby.values()); }
    Map<String, SpeechBackend.Provider<SpeechBackend.Asr>> asrOptions() { return asrBackends; }
    Map<String, SpeechBackend.Provider<SpeechBackend.Tts>> ttsOptions() { return ttsBackends; }

    SpeechBackend.Task checkMuse(Consumer<String> result) {
        return muse.check(SpeechBackend.result(
                value -> handler.post(() -> result.accept(value)),
                error -> handler.post(() -> result.accept(error))));
    }

    private void changed() { if (listener != null) listener.run(); }

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
        if (!settingPending) readSettings(this::snapshotChanged);
    }

    void updateSetting(int key, int value) {
        if (gatt == null || snapshot == null || settingPending) return;
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
        stopScan();
        handler.removeCallbacks(reconnect);
        handler.removeCallbacks(operationTimeout);
        operations.clear();
        running = null;
        rx = tx = settingsCharacteristic = null;
        speechReady = settingPending = false;
        snapshot = null;
        if (gatt != null) { gatt.close(); gatt = null; }
        if (audio != null) { audio.reset(); audio.features(0); }
        if (wake.isHeld()) wake.release();
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
                if (!connection.requestMtu(256)) discover(connection);
            });
        }
        @Override public void onMtuChanged(BluetoothGatt connection, int mtu, int code) {
            handler.post(() -> {
                if (gatt != connection) return;
                if (code == BluetoothGatt.GATT_SUCCESS) audio.mtu(mtu);
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
                    else update("本体設定を操作できます", "設定に接続");
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
                    readStatus(packet -> {
                        if (packet.type() == AudioPacket.REPLY_STATUS && packet.turn() == 0) audio.features(packet.offset());
                        reconnectDelay = 1_000;
                        update("C152の黄ボタンで会話", "会話できます");
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

    @Override public void retry(Runnable next) { handler.postDelayed(next, 20); }

    private void enqueue(Operation operation) {
        operations.addLast(operation);
        startNext();
    }

    private void startNext() {
        if (gatt == null || running != null || operations.isEmpty()) return;
        Operation operation = operations.removeFirst();
        running = operation;
        if (operation.start().getAsInt() != BluetoothStatusCodes.SUCCESS) {
            disconnect("StopWatchへの通信を開始できません"); return;
        }
        handler.postDelayed(operationTimeout, 25000);
    }

    private void complete(BluetoothGatt connection, int type, int code, byte[] value) {
        if (connection != gatt || running == null || running.type() != type) return;
        handler.removeCallbacks(operationTimeout);
        Operation operation = running;
        running = null;
        if (code != BluetoothGatt.GATT_SUCCESS) { disconnect("StopWatchとの通信でエラーが発生しました（" + code + "）"); return; }
        operation.complete().accept(value);
        startNext();
    }

}
