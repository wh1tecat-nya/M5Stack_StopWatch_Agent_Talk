package jp.sumi.stopwatch;

import javax.crypto.Cipher;
import javax.crypto.KeyAgreement;
import javax.crypto.Mac;
import javax.crypto.spec.GCMParameterSpec;
import javax.crypto.spec.SecretKeySpec;
import java.nio.ByteBuffer;
import java.nio.charset.StandardCharsets;
import java.security.KeyFactory;
import java.security.KeyPair;
import java.security.KeyPairGenerator;
import java.security.MessageDigest;
import java.security.spec.X509EncodedKeySpec;
import java.util.Arrays;

/** Client of the pinned Muse SDK's Noise_XX_25519_AESGCM_SHA256 protocol. */
final class MuseNoise {
    private static final byte[] EMPTY = new byte[0];
    private static final byte[] X25519_DER = {0x30, 0x2a, 0x30, 0x05, 0x06, 0x03, 0x2b, 0x65, 0x6e, 0x03, 0x21, 0};

    static final class Channel {
        private final SecretKeySpec key;
        private long nonce;

        Channel(byte[] key) { this.key = new SecretKeySpec(key, "AES"); }

        byte[] apply(int mode, byte[] ad, byte[] bytes) throws Exception {
            if (nonce == -1L) throw new IllegalStateException("Noise nonce exhausted");
            byte[] iv = ByteBuffer.allocate(12).putInt(0).putLong(nonce++).array();
            Cipher cipher = Cipher.getInstance("AES/GCM/NoPadding");
            cipher.init(mode, key, new GCMParameterSpec(128, iv));
            cipher.updateAAD(ad);
            return cipher.doFinal(bytes);
        }
    }

    private byte[] transcript;
    private byte[] chain;
    private Channel handshake;
    private KeyPair ephemeral;
    private Channel send;
    private Channel receive;

    MuseNoise() throws Exception {
        chain = Arrays.copyOf("Noise_XX_25519_AESGCM_SHA256".getBytes(StandardCharsets.US_ASCII), 32);
        transcript = chain;
        hash(EMPTY);
    }

    byte[] start() throws Exception {
        ephemeral = generate();
        byte[] key = publicKey(ephemeral);
        hash(key);
        hash(EMPTY);
        return key;
    }

    byte[] finish(byte[] response) throws Exception {
        if (response.length < 96 || response.length > 256) throw new IllegalArgumentException("Noise response length");
        byte[] remoteEphemeral = Arrays.copyOfRange(response, 0, 32);
        hash(remoteEphemeral);
        mix(dh(ephemeral, remoteEphemeral));
        byte[] staticKey = decryptAndHash(Arrays.copyOfRange(response, 32, 80));
        mix(dh(ephemeral, staticKey));
        decryptAndHash(Arrays.copyOfRange(response, 80, response.length));
        KeyPair localStatic = generate();
        byte[] encrypted = encryptAndHash(publicKey(localStatic));
        mix(dh(localStatic, remoteEphemeral));
        byte[] payload = encryptAndHash(EMPTY);
        byte[][] keys = hkdf(chain, EMPTY);
        send = new Channel(keys[0]);
        receive = new Channel(keys[1]);
        ephemeral = null;
        handshake = null;
        Arrays.fill(chain, (byte) 0);
        return join(encrypted, payload);
    }

    byte[] encrypt(byte[] bytes) throws Exception { return send.apply(Cipher.ENCRYPT_MODE, EMPTY, bytes); }
    byte[] decrypt(byte[] bytes) throws Exception { return receive.apply(Cipher.DECRYPT_MODE, EMPTY, bytes); }

    private void hash(byte[] data) throws Exception {
        var digest = MessageDigest.getInstance("SHA-256");
        digest.update(transcript);
        transcript = digest.digest(data);
    }

    private void mix(byte[] material) throws Exception {
        byte[][] keys = hkdf(chain, material);
        Arrays.fill(chain, (byte) 0);
        Arrays.fill(material, (byte) 0);
        chain = keys[0];
        handshake = new Channel(keys[1]);
    }

    private byte[] encryptAndHash(byte[] plaintext) throws Exception {
        byte[] encrypted = handshake.apply(Cipher.ENCRYPT_MODE, transcript, plaintext);
        hash(encrypted);
        return encrypted;
    }

    private byte[] decryptAndHash(byte[] bytes) throws Exception {
        byte[] decrypted = handshake.apply(Cipher.DECRYPT_MODE, transcript, bytes);
        hash(bytes);
        return decrypted;
    }

    private static byte[][] hkdf(byte[] key, byte[] material) throws Exception {
        Mac mac = Mac.getInstance("HmacSHA256");
        mac.init(new SecretKeySpec(key, "HmacSHA256"));
        byte[] temp = mac.doFinal(material);
        mac.init(new SecretKeySpec(temp, "HmacSHA256"));
        byte[] first = mac.doFinal(new byte[]{1});
        mac.update(first);
        return new byte[][]{first, mac.doFinal(new byte[]{2})};
    }

    private static KeyPair generate() throws Exception { return KeyPairGenerator.getInstance("X25519").generateKeyPair(); }

    private static byte[] dh(KeyPair local, byte[] peer) throws Exception {
        if (peer.length != 32) throw new IllegalArgumentException("Wrong public key length");
        var publicKey = KeyFactory.getInstance("X25519").generatePublic(new X509EncodedKeySpec(join(X25519_DER, peer)));
        KeyAgreement agree = KeyAgreement.getInstance("X25519");
        agree.init(local.getPrivate());
        agree.doPhase(publicKey, true);
        byte[] secret = agree.generateSecret();
        int any = 0;
        for (byte part : secret) any |= part;
        if (any == 0) throw new IllegalArgumentException("Invalid X25519 peer");
        return secret;
    }

    private static byte[] publicKey(KeyPair key) {
        byte[] encoded = key.getPublic().getEncoded();
        return Arrays.copyOfRange(encoded, encoded.length - 32, encoded.length);
    }

    private static byte[] join(byte[] one, byte[] two) {
        byte[] joined = Arrays.copyOf(one, one.length + two.length);
        System.arraycopy(two, 0, joined, one.length, two.length);
        return joined;
    }
}
