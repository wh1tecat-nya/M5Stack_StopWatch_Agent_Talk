package jp.sumi.stopwatch;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;

public final class ReconnectOnBoot extends BroadcastReceiver {
    @Override public void onReceive(Context context, Intent intent) {
        if (Intent.ACTION_BOOT_COMPLETED.equals(intent.getAction())) ConversationService.resumeSaved(context);
    }
}
