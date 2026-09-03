package com.kazumaproject.markdownhelperkeyboard.syncbridge

import android.content.Context
import android.util.AtomicFile
import java.io.File
import java.util.UUID

internal class DatabaseInstanceIdStore(context: Context) {
    private val idFile = AtomicFile(File(context.noBackupFilesDir, FILE_NAME))

    @Synchronized
    fun getOrCreate(): String {
        readValidId()?.let { return it }

        val newId = UUID.randomUUID().toString()
        val output = idFile.startWrite()
        try {
            output.write(newId.toByteArray(Charsets.UTF_8))
            idFile.finishWrite(output)
        } catch (error: Exception) {
            idFile.failWrite(output)
            throw error
        }
        return newId
    }

    private fun readValidId(): String? = try {
        idFile.openRead().bufferedReader(Charsets.UTF_8).use { reader ->
            reader.readText().trim().takeIf { value ->
                runCatching { UUID.fromString(value).toString() == value }.getOrDefault(false)
            }
        }
    } catch (_: Exception) {
        null
    }

    private companion object {
        const val FILE_NAME = "syncbridge-instance-id"
    }
}
