package jp.sumi.stopwatch;

import java.io.Closeable;
import java.io.IOException;
import java.net.HttpURLConnection;
import java.net.URI;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Future;

final class NetworkTask implements SpeechBackend.Task {
    interface Work<T> { T run(NetworkTask request) throws Exception; }
    static final class Failure extends IOException {
        Failure(String message) { super(message); }
    }

    private final Object lock = new Object();
    private volatile boolean cancelled;
    private Future<?> future;
    private Closeable resource;

    static <T> NetworkTask start(ExecutorService workers, SpeechBackend.Result<T> result, String error, Work<T> work) {
        NetworkTask request = new NetworkTask();
        request.future = workers.submit(() -> {
            try {
                T value = work.run(request);
                if (!request.cancelled) result.success(value);
            } catch (Failure e) {
                if (!request.cancelled) result.failure(e.getMessage());
            } catch (Exception e) {
                if (!request.cancelled) result.failure(error);
            } finally {
                request.release();
            }
        });
        return request;
    }

    HttpURLConnection open(URI uri) throws IOException {
        if (!"https".equals(uri.getScheme())) throw new IOException("HTTPS required");
        HttpURLConnection http = (HttpURLConnection) uri.toURL().openConnection();
        http.setConnectTimeout(10000);
        http.setReadTimeout(30000);
        http.setInstanceFollowRedirects(false);
        own(http::disconnect);
        return http;
    }

    void own(Closeable closeable) throws IOException {
        synchronized (lock) {
            closeResource();
            if (cancelled) {
                closeable.close();
                throw new IOException("Cancelled");
            }
            resource = closeable;
        }
    }

    void replace(Closeable closeable) throws IOException {
        synchronized (lock) {
            if (cancelled) {
                closeable.close();
                throw new IOException("Cancelled");
            }
            resource = closeable;
        }
    }

    private void release() {
        synchronized (lock) { closeResource(); }
    }

    private void closeResource() {
        if (resource == null) return;
        try { resource.close(); } catch (IOException ignored) {}
        resource = null;
    }

    @Override public void cancel() {
        cancelled = true;
        if (future != null) future.cancel(true);
        release();
    }
}
