package jp.sumi.stopwatch;

import android.content.Context;
import android.content.SharedPreferences;
import android.security.keystore.KeyGenParameterSpec;
import android.security.keystore.KeyProperties;
import android.util.Base64;

import java.nio.ByteBuffer;
import java.nio.charset.StandardCharsets;
import java.security.KeyStore;
import javax.crypto.Cipher;
import javax.crypto.KeyGenerator;
import javax.crypto.SecretKey;
import javax.crypto.spec.GCMParameterSpec;

final class LocalSecrets {
    private static final String ALIAS = "sumi-stopwatch-credentials";
    private final SharedPreferences store;

    LocalSecrets(Context context) {
        store = context.getSharedPreferences("credentials", Context.MODE_PRIVATE);
    }

    boolean has(String name) { return store.contains(name); }

    private synchronized SecretKey key() throws Exception {
        KeyStore keys = KeyStore.getInstance("AndroidKeyStore");
        keys.load(null);
        SecretKey existing = (SecretKey) keys.getKey(ALIAS, null);
        if (existing != null) return existing;
        KeyGenerator generator = KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, "AndroidKeyStore");
        generator.init(new KeyGenParameterSpec.Builder(ALIAS, KeyProperties.PURPOSE_ENCRYPT | KeyProperties.PURPOSE_DECRYPT)
                .setBlockModes(KeyProperties.BLOCK_MODE_GCM).setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
                .build());
        return generator.generateKey();
    }

    String read(String name) {
        String data = store.getString(name, "");
        if (data.isEmpty()) return "";
        try {
            ByteBuffer buffer = ByteBuffer.wrap(Base64.decode(data, Base64.NO_WRAP));
            byte[] iv = new byte[12];
            buffer.get(iv);
            byte[] ciphertext = new byte[buffer.remaining()];
            buffer.get(ciphertext);
            Cipher cipher = Cipher.getInstance("AES/GCM/NoPadding");
            cipher.init(Cipher.DECRYPT_MODE, key(), new GCMParameterSpec(128, iv));
            cipher.updateAAD(name.getBytes(StandardCharsets.UTF_8));
            return new String(cipher.doFinal(ciphertext), StandardCharsets.UTF_8);
        } catch (Exception e) { return ""; }
    }

    boolean save(String name, String value) {
        if (value.isEmpty()) return store.edit().remove(name).commit();
        try {
            Cipher cipher = Cipher.getInstance("AES/GCM/NoPadding");
            cipher.init(Cipher.ENCRYPT_MODE, key());
            cipher.updateAAD(name.getBytes(StandardCharsets.UTF_8));
            byte[] ciphertext = cipher.doFinal(value.getBytes(StandardCharsets.UTF_8));
            byte[] iv = cipher.getIV();
            byte[] data = ByteBuffer.allocate(iv.length + ciphertext.length).put(iv).put(ciphertext).array();
            return store.edit().putString(name, Base64.encodeToString(data, Base64.NO_WRAP)).commit();
        } catch (Exception e) { return false; }
    }
}
