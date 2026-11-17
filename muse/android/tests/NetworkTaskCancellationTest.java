package jp.sumi.stopwatch;

import java.util.concurrent.CountDownLatch;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicReference;

public final class NetworkTaskCancellationTest {
    public static void main(String[] args) throws Exception {
        var attached = new CountDownLatch(1);
        var closed = new CountDownLatch(1);
        var closedBy = new AtomicReference<Thread>();
        var closeCount = new AtomicInteger();
        var calls = Executors.newSingleThreadExecutor();
        try {
            var task = NetworkTask.start(calls, SpeechBackend.<Void>result(unused -> {}, unused -> {}), "failed", request -> {
                request.own(() -> {
                    closeCount.incrementAndGet();
                    closedBy.set(Thread.currentThread());
                    closed.countDown();
                });
                attached.countDown();
                closed.await();
                return null;
            });
            if (!attached.await(5, TimeUnit.SECONDS)) throw new AssertionError("Resource was never attached");
            task.cancel();
            task.cancel();
            if (!closed.await(5, TimeUnit.SECONDS)) throw new AssertionError("Cancel did not close resource");
            if (closedBy.get() == Thread.currentThread() || closeCount.get() != 1)
                throw new AssertionError("Cancellation must detach and close once off the caller thread");
        } finally {
            calls.shutdownNow();
        }
    }
}
