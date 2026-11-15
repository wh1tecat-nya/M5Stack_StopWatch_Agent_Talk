package jp.sumi.stopwatch;

import android.content.Context;
import jp.irodori.android.NativeEngine;

import java.io.ByteArrayOutputStream;
import java.io.File;
import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import java.nio.file.Files;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.Arrays;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Future;
import java.util.concurrent.atomic.AtomicBoolean;

final class IrodoriTts implements SpeechBackend.Tts, AutoCloseable {
    static final String MODEL = IrodoriModels.MF.name();
    static final String VOICE = "reference.voice.gguf";
    static final String ANIME_VOICE = "reference.anime.voice.gguf";
    private static final String MF_SOURCE = "https://huggingface.co/Aratako/Irodori-TTS-v4.1-Small-MF/tree/ccc78f5d480b6e51b69b2d5042a14c4da04fea6e";
    private static final String ANIME_SOURCE = "https://huggingface.co/phasefield-audio/Irodori-TTS-v4.1-Anime/tree/6b259f5baa5e236b3d14cbd1f8555ca87d92b530";
    private static final String REFERENCE = "d318b3ce9d4dc47eab4ead3270834e9bfc4cd0a5b4059c456677c5bc113da643";
    private static final String CODEC = "47376ee24834d7a05a48ebabfe3cde29b3c5e214";
    private final File files;
    private final ExecutorService worker;
    private volatile long engine;
    private int rate;
    private volatile boolean invalid;

    IrodoriTts(Context context, ExecutorService worker) {
        files = context.getFilesDir();
        this.worker = worker;
    }

    static String voiceFile(File voice) throws org.json.JSONException {
        byte[][] info = NativeEngine.referenceInfo(utf8(voice.getPath()));
        if (info == null || info.length != 7) return null;
        String source = new String(info[0], StandardCharsets.UTF_8);
        String name = switch (source) {
            case MF_SOURCE -> VOICE;
            case ANIME_SOURCE -> ANIME_VOICE;
            default -> null;
        };
        if (name == null) return null;
        String[] expected = {
                REFERENCE, "sumiref-speaker-encoder-cache", "1125", "282", "768"
        };
        for (int i = 0; i < expected.length; ++i) {
            if (!expected[i].equals(new String(info[i + 1], StandardCharsets.UTF_8))) return null;
        }
        var manifest = new org.json.JSONObject(new String(info[6], StandardCharsets.UTF_8));
        var clips = manifest.getJSONArray("clips");
        int[] frames = {79, 118, 78, 33, 38, 29, 116, 103, 163, 114, 168, 86};
        if (manifest.getInt("clipCount") != frames.length || clips.length() != frames.length) return null;
        int offset = 0;
        for (int i = 0; i < frames.length; i++) {
            var clip = clips.getJSONObject(i);
            if (clip.getInt("index") != i + 1 || clip.getInt("frameOffset") != offset ||
                    clip.getInt("frames") != frames[i]) return null;
            offset += frames[i];
        }
        return name;
    }

    static void convertReference(File model, File input, File output) throws Exception {
        if (IrodoriModels.bySize(model.length()) == null)
            throw new NetworkTask.Failure("Irodoriモデルを先に取り込んでください");
        if (input.length() > 256 * 1024)
            throw new NetworkTask.Failure("すみの参照ファイルが大きすぎます");
        byte[] bytes = Files.readAllBytes(input.toPath());
        StringBuilder hash = new StringBuilder(64);
        for (byte b : MessageDigest.getInstance("SHA-256").digest(bytes))
            hash.append(Character.forDigit((b & 255) >>> 4, 16)).append(Character.forDigit(b & 15, 16));
        if (!REFERENCE.contentEquals(hash) || !Arrays.equals(Arrays.copyOf(bytes, 8), utf8("SUMIRF01")))
            throw new NetworkTask.Failure("すみの参照ファイルと一致しません");
        ByteBuffer buffer = ByteBuffer.wrap(bytes).order(ByteOrder.LITTLE_ENDIAN);
        int metadata = buffer.getInt(8), payload = buffer.getInt(12);
        int offset = (16 + metadata + 3) & ~3;
        if (metadata <= 0 || offset > bytes.length || payload != 1125 * 32 * 4 || bytes.length - offset != payload)
            throw new NetworkTask.Failure("参照ファイルの長さが不正です");
        float[] latents = new float[payload / 4];
        buffer.position(offset);
        buffer.asFloatBuffer().get(latents);
        NativeEngine.makeSumirefVoice(utf8(model.getPath()), latents, utf8(CODEC),
                Arrays.copyOfRange(bytes, 16, 16 + metadata), utf8(REFERENCE), utf8(output.getPath()));
    }

    void reload() { invalid = true; }

    private long prepare() throws Exception {
        if (invalid) {
            invalid = false;
            closeEngine();
        }
        if (engine != 0) return engine;
        File modelFile = new File(files, MODEL);
        var model = IrodoriModels.bySize(modelFile.length());
        if (model == null) throw new NetworkTask.Failure("Irodoriモデルを取り込んでください");
        String expected = model.anime() ? ANIME_VOICE : VOICE;
        File voice = new File(files, expected);
        if (!voice.isFile() || !expected.equals(voiceFile(voice)))
            throw new NetworkTask.Failure("取り込んだモデル用のすみの参照を取り込んでください");
        int threads = Math.min(8, Runtime.getRuntime().availableProcessors());
        long loaded = NativeEngine.open(utf8(modelFile.getPath()), utf8(voice.getPath()),
                utf8("cpu-fast"), threads, model.anime(), true, model.anime() ? 6 : 4);
        if (loaded == 0) throw new NetworkTask.Failure("Irodoriのモデルを読み込めません");
        try {
            if (!model.anime()) {
                NativeEngine.configureMfProfile(loaded, 2);
                if (!"mf-candidate07-v1".equals(NativeEngine.mfProfileId(loaded)))
                    throw new IllegalStateException("Wrong Irodori profile");
                NativeEngine.configureMfAcceleration(loaded, 1, utf8(""), utf8(""), utf8(""));
            }
            rate = NativeEngine.sampleRate(loaded);
            ConversationLog.INSTANCE.event(ConversationLog.IRODORI, model.name() + " · " + threads + "スレッド");
            engine = loaded;
            return loaded;
        } catch (RuntimeException e) {
            NativeEngine.close(loaded);
            throw e;
        }
    }

    @Override public void warmUp() {
        worker.submit(() -> {
            try {
                prepare();
            } catch (Exception | UnsatisfiedLinkError | NoClassDefFoundError ignored) {
                // Synthesis retries and reports missing models and libraries through its result.
            }
        });
    }

    @Override public SpeechBackend.Task synthesize(SpeechBackend.Utterance utterance, SpeechBackend.Result<SpeechBackend.Audio> result) {
        AtomicBoolean cancelled = new AtomicBoolean();
        AtomicBoolean generating = new AtomicBoolean();
        Future<?> queued = worker.submit(() -> {
            if (cancelled.get()) return;
            try {
                long handle = prepare();
                if (cancelled.get()) return;
                NativeEngine.resetCancel(handle);
                generating.set(true);
                if (cancelled.get()) return;
                ByteArrayOutputStream out = new ByteArrayOutputStream();
                String text = utterance.text().replaceAll("[\\p{So}\\uFE0F\\u200D]+", "").strip();
                if (text.isEmpty()) throw new NetworkTask.Failure("読み上げる文字がありません");
                double[] stats = NativeEngine.speakWithCodec(handle, utf8(text), samples -> {
                    if (cancelled.get() || out.size() + samples.length * 2L > 240L * rate) return false;
                    byte[] pcm = new byte[samples.length * 2];
                    for (int i = 0; i < samples.length; i++) {
                        int sample = Math.round(Math.max(-1, Math.min(1, samples[i])) * 32767);
                        pcm[i * 2] = (byte) sample;
                        pcm[i * 2 + 1] = (byte) (sample >> 8);
                    }
                    out.writeBytes(pcm);
                    return true;
                }, 0, false);
                if (!cancelled.get()) {
                    if (out.size() == 0 || stats == null || stats[4] != 0) result.failure("Irodoriの音声生成が完了しませんでした");
                    else result.success(new SpeechBackend.Audio(out.toByteArray(), rate));
                }
            } catch (UnsatisfiedLinkError | NoClassDefFoundError e) {
                if (!cancelled.get()) result.failure("Irodori実行ライブラリを含むAPKをインストールしてください");
            } catch (NetworkTask.Failure e) {
                if (!cancelled.get()) result.failure(e.getMessage());
            } catch (Exception e) {
                if (!cancelled.get()) result.failure("Irodoriの生成に失敗しました。モデルとすみの参照を確認してください");
            } finally {
                generating.set(false);
            }
        });
        return () -> {
            cancelled.set(true);
            long handle = engine;
            if (handle != 0 && generating.get()) NativeEngine.cancel(handle);
            queued.cancel(false);
        };
    }

    private void closeEngine() {
        long handle = engine;
        engine = 0;
        if (handle != 0) NativeEngine.close(handle);
    }

    @Override public void close() { worker.submit(this::closeEngine); }

    private static byte[] utf8(String text) { return text.getBytes(StandardCharsets.UTF_8); }
}
