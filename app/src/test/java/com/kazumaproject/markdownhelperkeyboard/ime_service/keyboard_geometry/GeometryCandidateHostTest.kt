package com.kazumaproject.markdownhelperkeyboard.ime_service.keyboard_geometry

import android.content.Context
import android.graphics.Rect
import android.graphics.RectF
import android.widget.FrameLayout
import androidx.test.core.app.ApplicationProvider
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [24, 35])
class GeometryCandidateHostTest {
    private val context: Context = ApplicationProvider.getApplicationContext()

    @Test fun mapsScaledTranslatedKeyboardThroughScrolledAncestors() {
        val root = FrameLayout(context)
        val host = FrameLayout(context)
        val body = FrameLayout(context)
        root.addView(host)
        root.addView(body)
        root.layout(0, 0, 500, 500)
        host.layout(10, 20, 310, 320)
        body.layout(100, 120, 200, 220)
        root.scrollTo(3, 4)
        root.pivotX = 0f
        root.pivotY = 0f
        root.scaleX = 1.5f
        root.scaleY = 1.5f
        body.pivotX = 0f
        body.pivotY = 0f
        body.scaleX = 2f
        body.scaleY = 0.5f
        body.translationX = 5f
        body.translationY = 7f

        assertEquals(Rect(95, 107, 135, 127), geometryBoundsInHost(body, host, RectF(0f, 0f, 20f, 40f)))
    }

    @Test fun rejectsNonInvertibleHostTransform() {
        val host = FrameLayout(context).apply { scaleX = 0f }
        assertNull(geometryBoundsInHost(FrameLayout(context), host, RectF(0f, 0f, 20f, 40f)))
    }
}
