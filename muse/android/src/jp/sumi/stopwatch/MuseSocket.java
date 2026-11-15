package jp.sumi.stopwatch;

import javax.net.ssl.SSLSocket;
import javax.net.ssl.SSLSocketFactory;
import java.io.BufferedInputStream;
import java.io.BufferedOutputStream;
import java.io.ByteArrayOutputStream;
import java.io.Closeable;
import java.io.EOFException;
import java.io.IOException;
import java.io.InputStream;
import java.net.InetAddress;
import java.net.InetSocketAddress;
import java.net.Socket;
import java.net.URLEncoder;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.SecureRandom;
import java.util.Base64;
import java.util.Locale;

final class MuseSocket implements Closeable {
    private static final String HOST = "hatch.metaaivm.com";
    private static final int LIMIT = 256 * 1024;
    private static final ConversationLog LOG = ConversationLog.INSTANCE;
    private final SSLSocket socket;
    private final InputStream input;
    private final BufferedOutputStream output;
    private final SecureRandom random = new SecureRandom();

    MuseSocket(String vm, String token, NetworkTask task) throws Exception {
        if (!vm.matches("[A-Za-z0-9_-]{1,128}") || !token.matches("[!-~]{10,16384}")) throw new IOException("Bad VM credential");
        Socket connection = connect(task);
        socket = (SSLSocket) ((SSLSocketFactory) SSLSocketFactory.getDefault()).createSocket(
                connection, HOST, 443, true);
        task.replace(this);
        var parameters = socket.getSSLParameters();
        parameters.setEndpointIdentificationAlgorithm("HTTPS");
        socket.setSSLParameters(parameters);
        socket.setSoTimeout(10000);
        socket.startHandshake();
        LOG.event(ConversationLog.MUSE, "VM · TLS接続完了");
        input = new BufferedInputStream(socket.getInputStream());
        output = new BufferedOutputStream(socket.getOutputStream());
        byte[] nonce = new byte[16];
        random.nextBytes(nonce);
        String key = Base64.getEncoder().encodeToString(nonce);
        String accept = Base64.getEncoder().encodeToString(MessageDigest.getInstance("SHA-1")
                .digest((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").getBytes(StandardCharsets.US_ASCII)));
        String path = "/v1/noise?vm_id=" + URLEncoder.encode(vm, StandardCharsets.UTF_8) + "&app_id=hatch-web";
        output.write(("GET " + path + " HTTP/1.1\r\nHost: " + HOST + "\r\nAuthorization: Bearer " + token +
                "\r\nConnection: Upgrade\r\nUpgrade: websocket\r\nSec-WebSocket-Version: 13\r\nSec-WebSocket-Key: " +
                key + "\r\n\r\n").getBytes(StandardCharsets.US_ASCII));
        output.flush();
        String status = line(4096);
        if (!status.matches("HTTP/1\\.[01] 101( .*)?")) {
            String[] parts = status.split(" ", 3);
            String code = parts.length > 1 && parts[1].matches("[0-9]{3}") ? parts[1] : "不明";
            throw new NetworkTask.Failure("Muse VM接続エラー（HTTP " + code + "）");
        }
        boolean verified = false;
        int total = 0;
        for (String header; !(header = line(4096)).isEmpty();) {
            total += header.length();
            if (total > 16384) throw new IOException("Oversized WebSocket headers");
            if (header.toLowerCase(Locale.ROOT).startsWith("sec-websocket-accept:"))
                verified = accept.equals(header.substring(header.indexOf(':') + 1).strip());
        }
        if (!verified) throw new IOException("Invalid WebSocket upgrade");
        LOG.event(ConversationLog.MUSE, "VM · WebSocket HTTP 101");
    }

    private static Socket connect(NetworkTask task) throws IOException {
        InetAddress[] addresses = InetAddress.getAllByName(HOST);
        int remaining = 12_000;
        for (InetAddress address : addresses) {
            if (remaining <= 0) break;
            String family = address.getAddress().length == 4 ? "IPv4" : "IPv6";
            Socket candidate = new Socket();
            task.own(candidate);
            long start = System.nanoTime();
            try {
                candidate.connect(new InetSocketAddress(address, 443), Math.min(remaining, 2_500));
                LOG.event(ConversationLog.MUSE, "VM · " + family + " TCP接続完了");
                return candidate;
            } catch (IOException e) {
                candidate.close();
                LOG.event(ConversationLog.MUSE, "VM · " + family + " TCP接続失敗 · " + e.getClass().getSimpleName());
                remaining -= (int) ((System.nanoTime() - start) / 1_000_000);
            }
        }
        throw new NetworkTask.Failure("Muse VMに接続できません（TCP 443）");
    }

    private String line(int limit) throws IOException {
        ByteArrayOutputStream line = new ByteArrayOutputStream();
        for (int ch; (ch = input.read()) != -1;) {
            if (ch == '\n') return line.toString(StandardCharsets.US_ASCII);
            if (ch != '\r') line.write(ch);
            if (line.size() > limit) throw new IOException("Oversized WebSocket header");
        }
        throw new EOFException("WebSocket closed");
    }

    void send(byte[] data) throws IOException { frame(2, data); }

    private void frame(int opcode, byte[] data) throws IOException {
        output.write(0x80 | opcode);
        if (data.length < 126) output.write(0x80 | data.length);
        else if (data.length <= 65535) {
            output.write(0x80 | 126);
            number(data.length, 2);
        } else {
            output.write(0x80 | 127);
            number(data.length, 8);
        }
        byte[] mask = new byte[4];
        random.nextBytes(mask);
        output.write(mask);
        byte[] masked = new byte[data.length];
        for (int i = 0; i < data.length; i++) masked[i] = (byte) (data[i] ^ mask[i & 3]);
        output.write(masked);
        output.flush();
    }

    private void number(long value, int length) throws IOException {
        for (int i = length - 1; i >= 0; i--) output.write((int) (value >>> (i * 8)) & 255);
    }

    private int octet() throws IOException {
        int b = input.read();
        if (b < 0) throw new EOFException("WebSocket closed");
        return b;
    }

    byte[] receive() throws IOException {
        int first;
        try { first = octet(); }
        catch (java.net.SocketTimeoutException e) { return null; }
        ByteArrayOutputStream fragments = null;
        socket.setSoTimeout(10000);
        try {
            while (true) {
                int second = octet();
                boolean last = (first & 128) != 0;
                int opcode = first & 15;
                int length = second & 127;
                if ((first & 0x70) != 0 || (second & 128) != 0) throw new IOException("Invalid server frame");
                if (opcode >= 8 && (!last || length > 125)) throw new IOException("Invalid WebSocket control frame");
                if (length > 125) {
                    int size = length == 126 ? 2 : 8;
                    long raw = 0;
                    for (int i = 0; i < size; i++) raw = (raw << 8) | octet();
                    if (raw < 0 || raw > LIMIT) throw new IOException("WebSocket message too large");
                    length = (int) raw;
                }
                byte[] data = new byte[length];
                for (int offset = 0; offset < length;) {
                    int count = input.read(data, offset, length - offset);
                    if (count < 0) throw new EOFException("WebSocket closed");
                    offset += count;
                }
                if (opcode == 8) throw new EOFException("Muse closed WebSocket");
                if (opcode == 9) frame(10, data);
                else if (opcode == 2 && fragments == null) {
                    if (last) return data;
                    fragments = new ByteArrayOutputStream();
                    fragments.write(data);
                } else if (opcode == 0 && fragments != null) {
                    if (fragments.size() + data.length > LIMIT) throw new IOException("WebSocket message too large");
                    fragments.write(data);
                    if (last) return fragments.toByteArray();
                } else if (opcode != 10) throw new IOException("Unexpected WebSocket opcode");
                first = octet();
            }
        } finally {
            if (!socket.isClosed()) socket.setSoTimeout(1000);
        }
    }

    @Override public void close() throws IOException { socket.close(); }
}
