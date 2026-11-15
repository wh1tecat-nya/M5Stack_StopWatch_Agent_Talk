package jp.sumi.stopwatch;

import android.Manifest;
import android.content.Context;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.media.AudioFormat;
import android.os.Bundle;
import android.os.Handler;
import android.os.ParcelFileDescriptor;
import android.speech.RecognitionListener;
import android.speech.RecognizerIntent;
import android.speech.SpeechRecognizer;

import java.io.IOException;
import java.util.ArrayList;
import java.util.concurrent.ExecutorService;

final class AndroidAsr implements SpeechBackend.Asr {
    private final Context context;
    private final Handler main;
    private final ExecutorService workers;

    AndroidAsr(Context context, Handler main, ExecutorService workers) {
        this.context = context;
        this.main = main;
        this.workers = workers;
    }

    @Override public SpeechBackend.Task transcribe(SpeechBackend.Recording recording, SpeechBackend.Result<String> result) {
        if (!SpeechRecognizer.isOnDeviceRecognitionAvailable(context) ||
                context.checkSelfPermission(Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
            result.failure("Androidの端末内音声認識とマイク権限を確認してください");
            return () -> {};
        }
        try {
            ParcelFileDescriptor[] pipe = ParcelFileDescriptor.createPipe();
            Session session = new Session(pipe, result);
            try {
                session.start(recording);
                return session;
            } catch (RuntimeException e) {
                session.cancel();
                throw e;
            }
        } catch (IOException | RuntimeException e) {
            result.failure("AndroidにC152の録音を渡せません");
            return () -> {};
        }
    }

    private final class Session implements SpeechBackend.Task, RecognitionListener {
        private final ParcelFileDescriptor read, write;
        private final SpeechBackend.Result<String> result;
        private final Runnable timeout = () -> finish(null, "Android音声認識がタイムアウトしました");
        private final StringBuilder segments = new StringBuilder();
        private SpeechRecognizer recognizer;
        private boolean ended, sawSegment;

        Session(ParcelFileDescriptor[] pipe, SpeechBackend.Result<String> result) {
            read = pipe[0];
            write = pipe[1];
            this.result = result;
        }

        void start(SpeechBackend.Recording audio) {
            recognizer = SpeechRecognizer.createOnDeviceSpeechRecognizer(context);
            recognizer.setRecognitionListener(this);
            Intent intent = new Intent(RecognizerIntent.ACTION_RECOGNIZE_SPEECH)
                    .putExtra(RecognizerIntent.EXTRA_LANGUAGE_MODEL, RecognizerIntent.LANGUAGE_MODEL_FREE_FORM)
                    .putExtra(RecognizerIntent.EXTRA_LANGUAGE, audio.language())
                    .putExtra(RecognizerIntent.EXTRA_AUDIO_SOURCE, read)
                    .putExtra(RecognizerIntent.EXTRA_AUDIO_SOURCE_CHANNEL_COUNT, 1)
                    .putExtra(RecognizerIntent.EXTRA_AUDIO_SOURCE_ENCODING, AudioFormat.ENCODING_PCM_16BIT)
                    .putExtra(RecognizerIntent.EXTRA_AUDIO_SOURCE_SAMPLING_RATE, audio.sampleRate())
                    .putExtra(RecognizerIntent.EXTRA_SEGMENTED_SESSION, RecognizerIntent.EXTRA_AUDIO_SOURCE);
            recognizer.startListening(intent);
            main.postDelayed(timeout, 25000);
            workers.execute(() -> {
                try (var sink = new ParcelFileDescriptor.AutoCloseOutputStream(write)) {
                    sink.write(audio.pcm16());
                } catch (IOException e) {
                    main.post(() -> finish(null, "Android音声認識がC152の録音を読み取れません"));
                }
            });
        }

        @Override public void onSegmentResults(Bundle bundle) {
            sawSegment = true;
            String text = first(bundle);
            if (!text.isEmpty()) {
                if (segments.length() > 0) segments.append(' ');
                segments.append(text);
            }
        }

        @Override public void onEndOfSegmentedSession() {
            finish(sawSegment ? segments.toString() : null, "Android音声認識が録音入力に対応していません");
        }

        @Override public void onResults(Bundle bundle) {
            // Only accept input acknowledged by the file-descriptor segmented session.
            // Some recognition services silently switch to the handset microphone.
            if (!sawSegment) finish(null, "Android音声認識が録音入力に対応していません");
        }

        @Override public void onError(int error) {
            finish(null, "Android音声認識エラー（" + error + "）");
        }

        private String first(Bundle bundle) {
            ArrayList<String> texts = bundle.getStringArrayList(SpeechRecognizer.RESULTS_RECOGNITION);
            return texts == null || texts.isEmpty() ? "" : texts.get(0);
        }

        private void finish(String text, String error) {
            if (ended) return;
            cancel();
            if (text == null) result.failure(error);
            else result.success(text);
        }

        @Override public void cancel() {
            if (ended) return;
            ended = true;
            main.removeCallbacks(timeout);
            if (recognizer != null) {
                recognizer.cancel();
                recognizer.destroy();
            }
            try { read.close(); } catch (IOException ignored) {}
            try { write.close(); } catch (IOException ignored) {}
        }

        @Override public void onReadyForSpeech(Bundle params) {}
        @Override public void onBeginningOfSpeech() {}
        @Override public void onRmsChanged(float rmsdB) {}
        @Override public void onBufferReceived(byte[] buffer) {}
        @Override public void onEndOfSpeech() {}
        @Override public void onPartialResults(Bundle partialResults) {}
        @Override public void onEvent(int eventType, Bundle params) {}
    }
}
