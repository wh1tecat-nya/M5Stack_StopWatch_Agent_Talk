package jp.irodori.android;

/** JNI ABI used by the verified Irodori Offline v0.45.0 arm64 runtime. */
public final class NativeEngine {
    static { System.loadLibrary("irodori-jni"); }
    public interface Sink { boolean onAudio(float[] samples); }
    public static native long open(byte[] model, byte[] voice, byte[] device, int threads,
                                   boolean anime, boolean reuseConditions, int steps);
    public static native void configureMfProfile(long engine, int profile);
    public static native String mfProfileId(long engine);
    public static native void configureMfAcceleration(long engine, int mode, byte[] directory, byte[] library, byte[] cache);
    public static native byte[][] referenceInfo(byte[] path);
    public static native void makeSumirefVoice(byte[] model, float[] latent, byte[] codecRevision,
                                              byte[] manifest, byte[] sourceHash, byte[] output);
    public static native int sampleRate(long engine);
    public static native void cancel(long engine);
    public static native void resetCancel(long engine);
    public static native void close(long engine);
    public static native double[] speakWithCodec(long engine, byte[] text, Sink sink, int codecFrames, boolean notifyProgress);

    private NativeEngine() {}
}
