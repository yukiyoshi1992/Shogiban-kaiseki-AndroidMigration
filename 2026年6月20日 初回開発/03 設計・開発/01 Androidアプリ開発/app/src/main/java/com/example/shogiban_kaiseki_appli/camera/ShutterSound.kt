package com.example.shogiban_kaiseki_appli.camera

import android.media.MediaActionSound
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.remember

/**
 * シャッター音再生用。撮影できたかどうかが分からないというフィードバック
 * （Discord 2026-06-21、CLAUDE.md「今後の検討事項」No.7）への対応。
 * 標準のカメラシャッター音（MediaActionSound.SHUTTER_CLICK）を撮影トリガー時に再生する。
 */
@Composable
fun rememberShutterSound(): () -> Unit {
    val sound = remember { MediaActionSound() }
    DisposableEffect(Unit) {
        sound.load(MediaActionSound.SHUTTER_CLICK)
        onDispose { sound.release() }
    }
    return { sound.play(MediaActionSound.SHUTTER_CLICK) }
}
