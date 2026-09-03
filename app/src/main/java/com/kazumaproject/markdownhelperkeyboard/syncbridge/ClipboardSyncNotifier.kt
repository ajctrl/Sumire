package com.kazumaproject.markdownhelperkeyboard.syncbridge

import android.content.Context
import android.content.Intent
import com.kazumaproject.markdownhelperkeyboard.BuildConfig

object ClipboardSyncNotifier {
    fun notifyClipboardChanged(context: Context) {
        val syncPackage = BuildConfig.SUMIRE_SYNC_PACKAGE_ID.trim()
        if (syncPackage.isEmpty()) return

        val intent = Intent(ClipboardSyncContract.changedAction(context)).apply {
            setPackage(syncPackage)
        }
        context.sendBroadcast(intent, ClipboardSyncContract.permission(context))
    }
}
