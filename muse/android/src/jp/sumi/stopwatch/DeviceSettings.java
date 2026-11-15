package jp.sumi.stopwatch;

import java.nio.ByteBuffer;
import java.nio.ByteOrder;

final class DeviceSettings {
    record Snapshot(int sequence, int result, int[] values) {
        static Snapshot read(byte[] packet) {
            if (packet.length < 5) throw new IllegalArgumentException("Short settings packet");
            ByteBuffer in = ByteBuffer.wrap(packet).order(ByteOrder.LITTLE_ENDIAN);
            int version = Byte.toUnsignedInt(in.get());
            int sequence = Short.toUnsignedInt(in.getShort());
            int result = Byte.toUnsignedInt(in.get());
            int count = Byte.toUnsignedInt(in.get());
            if (version != 1 || count != FIELDS.length || in.remaining() != count * 2)
                throw new IllegalArgumentException("Unexpected settings version");
            int[] values = new int[count];
            for (int i = 0; i < count; i++) values[i] = Short.toUnsignedInt(in.getShort());
            return new Snapshot(sequence, result, values);
        }
    }

    record Field(String title, int[] options, String[] labels, int min, int max, String unit) {
        Field(String title, int[] options, String... labels) { this(title, options, labels, 0, 0, ""); }
        Field(String title, int min, int max, String unit) { this(title, null, null, min, max, unit); }

        String describe(int value) {
            if (options != null) for (int i = 0; i < options.length; i++) if (options[i] == value) return labels[i];
            return value + unit;
        }
    }

    private static final int[] BINARY = {0, 1};
    private static final int[] TIMES = {15, 30, 45, 60};
    private static final String[] SECONDS = {"15秒", "30秒", "45秒", "60秒"};
    static final Field[] FIELDS = {
        new Field("接続モード", BINARY, "Wi-Fi（本体）", "BLE（スマホ）"),
        new Field("会話先", BINARY, "メインチャット", "M5Stack会話用"),
        new Field("mcu-wearable", BINARY, "OFF", "ON"),
        new Field("音量", 0, 100, "%"),
        new Field("マイク感度", new int[]{0,3,6,9,12,15,18,21,24,27,30,33,36},
                "0 dB","3 dB","6 dB","9 dB","12 dB","15 dB","18 dB","21 dB","24 dB","27 dB","30 dB","33 dB","36 dB"),
        new Field("本体TTSの速度", new int[]{60,70,80,90,100,110,120}, "0.6倍","0.7倍","0.8倍","0.9倍","1.0倍","1.1倍","1.2倍"),
        new Field("Wi-Fiモードの音声認識", BINARY, "Muse", "OpenAI"),
        new Field("アバターの明るさ", 1, 100, "%"),
        new Field("バッジの明るさ", 1, 100, "%"),
        new Field("アバターの表示時間", TIMES, SECONDS),
        new Field("明るいバッジの表示時間", TIMES, SECONDS),
        new Field("暗いバッジの表示時間", TIMES, SECONDS),
        new Field("充電速度", new int[]{185,425}, "標準 185mA", "高速 425mA"),
        new Field("Wi-Fi設定用ネットワーク", BINARY, "閉じる", "開始"),
    };

    static byte[] command(int sequence, int key, int value) {
        return ByteBuffer.allocate(6).order(ByteOrder.LITTLE_ENDIAN).put((byte) 1)
                .putShort((short) sequence).put((byte) key).putShort((short) value).array();
    }

    private DeviceSettings() {}
}
