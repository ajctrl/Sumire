package com.kazumaproject.markdownhelperkeyboard.syncbridge

import android.content.Context
import androidx.test.core.app.ApplicationProvider
import org.junit.Assert.assertEquals
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner

@RunWith(RobolectricTestRunner::class)
class ClipboardSyncContractTest {
    private val context: Context = ApplicationProvider.getApplicationContext()

    @Test
    fun authorityPermissionAndActionFollowRuntimePackageName() {
        val packageName = context.packageName

        assertEquals("$packageName.syncbridge.clipboard", ClipboardSyncContract.authority(context))
        assertEquals(
            "$packageName.permission.CLIPBOARD_SYNC",
            ClipboardSyncContract.permission(context)
        )
        assertEquals(
            "$packageName.action.CLIPBOARD_CHANGED",
            ClipboardSyncContract.changedAction(context)
        )
    }

    @Test
    fun contentUriUsesStableItemStreamPath() {
        assertEquals(
            "content://${context.packageName}.syncbridge.clipboard/items/1253/content",
            ClipboardSyncContract.contentUri(context, 1253L).toString()
        )
    }

    @Test
    fun providerVersionsStartAtOne() {
        assertEquals(1, ClipboardSyncContract.API_VERSION)
        assertEquals(1, ClipboardSyncContract.CLIPBOARD_GENERATION)
        assertEquals(500, ClipboardSyncContract.MAX_LIMIT)
    }
}
