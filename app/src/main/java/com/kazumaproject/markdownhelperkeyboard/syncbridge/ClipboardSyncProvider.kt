package com.kazumaproject.markdownhelperkeyboard.syncbridge

import android.content.ContentProvider
import android.content.ContentValues
import android.content.UriMatcher
import android.database.Cursor
import android.database.MatrixCursor
import android.net.Uri
import android.os.ParcelFileDescriptor
import com.kazumaproject.markdownhelperkeyboard.database.AppDatabase
import dagger.hilt.android.EntryPointAccessors
import java.io.File
import java.io.FileNotFoundException
import java.io.IOException

/**
 * Signature-protected, read-only access to clipboard history for Sumire Sync.
 *
 * This deliberately queries SQLite through Room's readable open helper so the
 * upstream clipboard DAO, entity and schema remain unchanged.
 */
class ClipboardSyncProvider : ContentProvider() {
    private lateinit var database: AppDatabase
    private lateinit var instanceIdStore: DatabaseInstanceIdStore
    private lateinit var uriMatcher: UriMatcher

    override fun onCreate(): Boolean {
        val appContext = context?.applicationContext ?: return false
        database = EntryPointAccessors.fromApplication(
            appContext,
            ClipboardSyncEntryPoint::class.java
        ).appDatabase()
        instanceIdStore = DatabaseInstanceIdStore(appContext)
        uriMatcher = UriMatcher(UriMatcher.NO_MATCH).apply {
            val authority = ClipboardSyncContract.authority(appContext)
            addURI(authority, ClipboardSyncContract.PATH_ITEMS, MATCH_ITEMS)
            addURI(authority, ClipboardSyncContract.PATH_STATUS, MATCH_STATUS)
            addURI(
                authority,
                "${ClipboardSyncContract.PATH_ITEMS}/#/${ClipboardSyncContract.PATH_CONTENT}",
                MATCH_CONTENT
            )
        }
        return true
    }

    override fun query(
        uri: Uri,
        projection: Array<out String>?,
        selection: String?,
        selectionArgs: Array<out String>?,
        sortOrder: String?
    ): Cursor = when (uriMatcher.match(uri)) {
        MATCH_ITEMS -> queryItems(uri, projection)
        MATCH_STATUS -> queryStatus(projection)
        else -> throw IllegalArgumentException("Unsupported URI: $uri")
    }

    override fun getType(uri: Uri): String? = when (uriMatcher.match(uri)) {
        MATCH_ITEMS -> MIME_ITEMS
        MATCH_STATUS -> MIME_STATUS
        MATCH_CONTENT -> contentType(uri)
        else -> null
    }

    override fun openFile(uri: Uri, mode: String): ParcelFileDescriptor {
        if (uriMatcher.match(uri) != MATCH_CONTENT) {
            throw FileNotFoundException("Unsupported URI: $uri")
        }
        if (mode != "r") {
            throw FileNotFoundException("Clipboard content is read-only")
        }

        val id = itemId(uri)
        val path = findContent(id)?.second
            ?: throw FileNotFoundException("Clipboard item $id does not exist")
        val file = validatedContentFile(path)
        return ParcelFileDescriptor.open(file, ParcelFileDescriptor.MODE_READ_ONLY)
    }

    override fun insert(uri: Uri, values: ContentValues?): Uri = readOnly()

    override fun delete(uri: Uri, selection: String?, selectionArgs: Array<out String>?): Int =
        readOnly()

    override fun update(
        uri: Uri,
        values: ContentValues?,
        selection: String?,
        selectionArgs: Array<out String>?
    ): Int = readOnly()

    private fun queryItems(uri: Uri, projection: Array<out String>?): Cursor {
        val columns = validatedProjection(projection, ClipboardSyncContract.ITEM_COLUMNS)
        val afterId = uri.getQueryParameter(ClipboardSyncContract.PARAM_AFTER_ID)
            ?.toLongOrNull()
            ?.coerceAtLeast(0L)
            ?: 0L
        val limit = uri.getQueryParameter(ClipboardSyncContract.PARAM_LIMIT)
            ?.toIntOrNull()
            ?.coerceIn(1, ClipboardSyncContract.MAX_LIMIT)
            ?: ClipboardSyncContract.DEFAULT_LIMIT

        val result = MatrixCursor(columns)
        val db = database.openHelper.readableDatabase
        db.query(
            """
            SELECT id, itemType, timestamp AS createdAt, isPinned, preview
            FROM clipboard_history
            WHERE id > ?
            ORDER BY id ASC
            LIMIT ?
            """.trimIndent(),
            arrayOf(afterId.toString(), limit.toString())
        ).use { source ->
            val idIndex = source.getColumnIndexOrThrow(ClipboardSyncContract.COLUMN_ID)
            val typeIndex = source.getColumnIndexOrThrow(ClipboardSyncContract.COLUMN_ITEM_TYPE)
            val createdAtIndex = source.getColumnIndexOrThrow(ClipboardSyncContract.COLUMN_CREATED_AT)
            val pinnedIndex = source.getColumnIndexOrThrow(ClipboardSyncContract.COLUMN_IS_PINNED)
            val previewIndex = source.getColumnIndexOrThrow(ClipboardSyncContract.COLUMN_PREVIEW)
            while (source.moveToNext()) {
                val id = source.getLong(idIndex)
                val values = mapOf<String, Any?>(
                    ClipboardSyncContract.COLUMN_ID to id,
                    ClipboardSyncContract.COLUMN_ITEM_TYPE to source.getString(typeIndex),
                    ClipboardSyncContract.COLUMN_CREATED_AT to source.getLong(createdAtIndex),
                    ClipboardSyncContract.COLUMN_IS_PINNED to source.getInt(pinnedIndex),
                    ClipboardSyncContract.COLUMN_PREVIEW to source.getString(previewIndex),
                    ClipboardSyncContract.COLUMN_CONTENT_URI to
                        ClipboardSyncContract.contentUri(providerContext(), id).toString()
                )
                result.addRow(columns.map(values::get))
            }
        }
        result.setNotificationUri(providerContext().contentResolver, uri)
        return result
    }

    private fun queryStatus(projection: Array<out String>?): Cursor {
        val columns = validatedProjection(projection, ClipboardSyncContract.STATUS_COLUMNS)
        val values = mapOf<String, Any?>(
            ClipboardSyncContract.COLUMN_API_VERSION to ClipboardSyncContract.API_VERSION,
            ClipboardSyncContract.COLUMN_DATABASE_INSTANCE_ID to instanceIdStore.getOrCreate(),
            ClipboardSyncContract.COLUMN_CLIPBOARD_GENERATION to
                ClipboardSyncContract.CLIPBOARD_GENERATION,
            ClipboardSyncContract.COLUMN_CURRENT_SEQUENCE to currentSequence()
        )
        return MatrixCursor(columns, 1).apply { addRow(columns.map(values::get)) }
    }

    private fun currentSequence(): Long {
        val db = database.openHelper.readableDatabase
        db.query(
            "SELECT seq FROM sqlite_sequence WHERE name = ?",
            arrayOf(CLIPBOARD_TABLE)
        ).use { cursor ->
            return if (cursor.moveToFirst()) cursor.getLong(0) else 0L
        }
    }

    private fun contentType(uri: Uri): String? = findContent(itemId(uri))?.first?.let { itemType ->
        when (itemType) {
            ITEM_TYPE_TEXT -> "text/plain"
            ITEM_TYPE_IMAGE -> "image/png"
            else -> null
        }
    }

    private fun findContent(id: Long): Pair<String, String>? {
        val db = database.openHelper.readableDatabase
        db.query(
            "SELECT itemType, contentPath FROM clipboard_history WHERE id = ? LIMIT 1",
            arrayOf(id.toString())
        ).use { cursor ->
            if (!cursor.moveToFirst()) return null
            return cursor.getString(0) to cursor.getString(1)
        }
    }

    private fun validatedContentFile(path: String): File {
        val appContext = providerContext()
        val root: File
        val file: File
        try {
            root = File(appContext.filesDir, CLIPBOARD_DIRECTORY).canonicalFile
            file = File(path).canonicalFile
        } catch (error: IOException) {
            throw FileNotFoundException("Clipboard content is unavailable").apply {
                initCause(error)
            }
        }
        val isWithinClipboardDirectory = file.path.startsWith(root.path + File.separator)
        if (!isWithinClipboardDirectory || !file.isFile) {
            throw FileNotFoundException("Clipboard content is unavailable")
        }
        return file
    }

    private fun itemId(uri: Uri): Long = uri.pathSegments.getOrNull(1)?.toLongOrNull()
        ?.takeIf { it > 0L }
        ?: throw IllegalArgumentException("Invalid clipboard item URI: $uri")

    private fun validatedProjection(
        requested: Array<out String>?,
        allowed: Array<String>
    ): Array<String> {
        val columns = requested?.map { it }?.toTypedArray() ?: allowed.copyOf()
        require(columns.all(allowed::contains)) { "Unsupported projection" }
        return columns
    }

    private fun providerContext() = checkNotNull(context) { "Provider is not attached" }

    private fun <T> readOnly(): T = throw UnsupportedOperationException("Provider is read-only")

    private companion object {
        const val MATCH_ITEMS = 1
        const val MATCH_STATUS = 2
        const val MATCH_CONTENT = 3

        const val CLIPBOARD_TABLE = "clipboard_history"
        const val CLIPBOARD_DIRECTORY = "clipboard_history"
        const val ITEM_TYPE_TEXT = "TEXT"
        const val ITEM_TYPE_IMAGE = "IMAGE"

        const val MIME_ITEMS = "vnd.android.cursor.dir/vnd.sumire.clipboard-item"
        const val MIME_STATUS = "vnd.android.cursor.item/vnd.sumire.clipboard-status"
    }
}
