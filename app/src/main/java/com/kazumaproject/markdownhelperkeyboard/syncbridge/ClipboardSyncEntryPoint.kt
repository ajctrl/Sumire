package com.kazumaproject.markdownhelperkeyboard.syncbridge

import com.kazumaproject.markdownhelperkeyboard.database.AppDatabase
import dagger.hilt.EntryPoint
import dagger.hilt.InstallIn
import dagger.hilt.components.SingletonComponent

@EntryPoint
@InstallIn(SingletonComponent::class)
interface ClipboardSyncEntryPoint {
    fun appDatabase(): AppDatabase
}
