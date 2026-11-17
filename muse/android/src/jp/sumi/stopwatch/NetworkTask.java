package jp.sumi.stopwatch;

import java.io.Closeable;
import java.io.IOException;
import java.net.HttpURLConnection;
import java.net.URI;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;

final class NetworkTask implements SpeechBackend.Task {
    private static final ExecutorService CLOSER = Executors.newCachedThreadPool(runnable -> {
        Thread thread = new Thread(runnable, "sumi-network-close");
        thread.setDaemon(true);
        return thread;
    });
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
        Closeable previous;
        boolean accepted;
        synchronized (lock) {
            previous = resource;
            accepted = !cancelled;
            resource = accepted ? closeable : null;
        }
        close(previous);
        if (!accepted) {
            close(closeable);
            throw new IOException("Cancelled");
        }
    }

    void replace(Closeable closeable) throws IOException {
        synchronized (lock) {
            if (!cancelled) {
                resource = closeable;
                return;
            }
        }
        close(closeable);
        throw new IOException("Cancelled");
    }

    private void release() {
        close(takeResource());
    }

    private Closeable takeResource() {
        synchronized (lock) {
            Closeable active = resource;
            resource = null;
            return active;
        }
    }

    private static void close(Closeable active) {
        if (active == null) return;
        try { active.close(); } catch (IOException ignored) {}
    }

    @Override public void cancel() {
        cancelled = true;
        Closeable active = takeResource();
        if (future != null) future.cancel(true);
        if (active != null) CLOSER.execute(() -> close(active));
    }
}
