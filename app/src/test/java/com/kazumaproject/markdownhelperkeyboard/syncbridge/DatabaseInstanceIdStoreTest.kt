package com.kazumaproject.markdownhelperkeyboard.syncbridge

import android.content.Context
import androidx.test.core.app.ApplicationProvider
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertNotNull
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.annotation.Config
import org.robolectric.RobolectricTestRunner
import java.io.File
import java.util.UUID

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [35])
class DatabaseInstanceIdStoreTest {
    private val context: Context = ApplicationProvider.getApplicationContext()
    private val idFile by lazy { File(context.noBackupFilesDir, "syncbridge-instance-id") }

    @Test
    fun instanceIdIsStableAndValid() {
        idFile.delete()
        val store = DatabaseInstanceIdStore(context)

        val first = store.getOrCreate()
        val second = store.getOrCreate()

        assertEquals(first, second)
        assertNotNull(UUID.fromString(first))
    }

    @Test
    fun invalidStoredValueIsReplaced() {
        idFile.parentFile?.mkdirs()
        idFile.writeText("not-a-uuid")

        val replacement = DatabaseInstanceIdStore(context).getOrCreate()

        assertNotEquals("not-a-uuid", replacement)
        assertNotNull(UUID.fromString(replacement))
    }
}
