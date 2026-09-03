package com.kazumaproject.markdownhelperkeyboard.syncbridge

import android.content.Context
import android.net.Uri

/** Stable IPC contract shared with the separately built Sumire Sync app. */
object ClipboardSyncContract {
    const val API_VERSION = 1
    const val CLIPBOARD_GENERATION = 1

    const val AUTHORITY_SUFFIX = ".syncbridge.clipboard"
    const val PERMISSION_SUFFIX = ".permission.CLIPBOARD_SYNC"
    const val ACTION_SUFFIX = ".action.CLIPBOARD_CHANGED"

    const val PATH_ITEMS = "items"
    const val PATH_STATUS = "status"
    const val PATH_CONTENT = "content"

    const val PARAM_AFTER_ID = "afterId"
    const val PARAM_LIMIT = "limit"
    const val DEFAULT_LIMIT = 100
    const val MAX_LIMIT = 500

    const val COLUMN_ID = "id"
    const val COLUMN_ITEM_TYPE = "itemType"
    const val COLUMN_CREATED_AT = "createdAt"
    const val COLUMN_IS_PINNED = "isPinned"
    const val COLUMN_PREVIEW = "preview"
    const val COLUMN_CONTENT_URI = "contentUri"

    const val COLUMN_API_VERSION = "apiVersion"
    const val COLUMN_DATABASE_INSTANCE_ID = "databaseInstanceId"
    const val COLUMN_CLIPBOARD_GENERATION = "clipboardGeneration"
    const val COLUMN_CURRENT_SEQUENCE = "currentSequence"

    val ITEM_COLUMNS = arrayOf(
        COLUMN_ID,
        COLUMN_ITEM_TYPE,
        COLUMN_CREATED_AT,
        COLUMN_IS_PINNED,
        COLUMN_PREVIEW,
        COLUMN_CONTENT_URI
    )

    val STATUS_COLUMNS = arrayOf(
        COLUMN_API_VERSION,
        COLUMN_DATABASE_INSTANCE_ID,
        COLUMN_CLIPBOARD_GENERATION,
        COLUMN_CURRENT_SEQUENCE
    )

    fun authority(context: Context): String = context.packageName + AUTHORITY_SUFFIX

    fun permission(context: Context): String = context.packageName + PERMISSION_SUFFIX

    fun changedAction(context: Context): String = context.packageName + ACTION_SUFFIX

    fun itemsUri(context: Context): Uri = Uri.Builder()
        .scheme("content")
        .authority(authority(context))
        .appendPath(PATH_ITEMS)
        .build()

    fun contentUri(context: Context, id: Long): Uri = itemsUri(context).buildUpon()
        .appendPath(id.toString())
        .appendPath(PATH_CONTENT)
        .build()

    fun statusUri(context: Context): Uri = Uri.Builder()
        .scheme("content")
        .authority(authority(context))
        .appendPath(PATH_STATUS)
        .build()
}
