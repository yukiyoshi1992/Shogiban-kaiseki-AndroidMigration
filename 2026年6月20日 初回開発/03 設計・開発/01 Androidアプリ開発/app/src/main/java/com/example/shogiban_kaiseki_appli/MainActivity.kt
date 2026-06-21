package com.example.shogiban_kaiseki_appli

import android.Manifest
import android.content.pm.PackageManager
import android.os.Bundle
import android.speech.tts.TextToSpeech
import android.view.KeyEvent
import android.view.WindowManager
import android.widget.Toast
import androidx.activity.ComponentActivity
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.Button
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.core.content.ContextCompat
import com.example.shogiban_kaiseki_appli.calibration.CalibrationScreen
import com.example.shogiban_kaiseki_appli.play.PlayScreen
import com.example.shogiban_kaiseki_appli.ui.theme.ShogibankaisekiappliTheme
import java.util.Locale

class MainActivity : ComponentActivity() {
    private var tts: TextToSpeech? = null
    private var shutterTrigger: (() -> Unit)? = null

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge()
        tts = TextToSpeech(this) { status ->
            if (status == TextToSpeech.SUCCESS) {
                tts?.setLanguage(Locale.JAPANESE)
            }
        }
        setContent {
            ShogibankaisekiappliTheme {
                Scaffold(modifier = Modifier.fillMaxSize()) { innerPadding ->
                    CameraPermissionGate(modifier = Modifier.padding(innerPadding)) { contentModifier ->
                        ShogiAppFlow(
                            modifier = contentModifier,
                            tts = tts,
                            registerShutterTrigger = { trigger -> shutterTrigger = trigger },
                            setKeepScreenOn = { on -> setKeepScreenOn(on) }
                        )
                    }
                }
            }
        }
    }

    private fun setKeepScreenOn(on: Boolean) {
        if (on) {
            window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        } else {
            window.clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        }
    }

    // Bluetoothシャッター（音量キー押下として届く）をシャッタートリガーにフックする
    // （アーキテクチャ検討.md 4節）。キャリブレーション撮影・対局中の両方で使えるよう
    // 音量アップ・ダウンの両方を対象にする（2026-06-21、ユーザー要望）。
    // onKeyDownではなくdispatchKeyEventで拾う：onKeyDownはディスパッチ経路の後段で呼ばれる
    // ため、Compose側のフォーカス処理等に先に消費されて届かないことがある。dispatchKeyEventは
    // ディスパッチの最初に呼ばれるため確実に拾える（実機テストでonKeyDownが反応しなかったため変更）。
    override fun dispatchKeyEvent(event: KeyEvent): Boolean {
        if (event.action == KeyEvent.ACTION_DOWN &&
            (event.keyCode == KeyEvent.KEYCODE_VOLUME_UP || event.keyCode == KeyEvent.KEYCODE_VOLUME_DOWN)
        ) {
            val trigger = shutterTrigger
            // 音量キーが反応しないという報告の原因調査用。イベント自体が届いているか、
            // シャッターが登録済みかをトーストで可視化する（2026-06-21、原因切り分け用の一時的な診断）。
            Toast.makeText(
                this,
                if (trigger != null) "音量キー検知→シャッター実行" else "音量キー検知したがシャッター未登録",
                Toast.LENGTH_SHORT
            ).show()
            if (trigger != null) {
                trigger.invoke()
                return true
            }
        }
        return super.dispatchKeyEvent(event)
    }

    override fun onDestroy() {
        tts?.shutdown()
        super.onDestroy()
    }
}

private sealed class AppScreen {
    object Calibration : AppScreen()
    object Playing : AppScreen()
}

@Composable
private fun ShogiAppFlow(
    modifier: Modifier = Modifier,
    tts: TextToSpeech?,
    registerShutterTrigger: (() -> Unit) -> Unit,
    setKeepScreenOn: (Boolean) -> Unit
) {
    var screen by remember { mutableStateOf<AppScreen>(AppScreen.Calibration) }

    when (screen) {
        is AppScreen.Calibration -> {
            DisposableEffect(Unit) {
                setKeepScreenOn(false)
                onDispose {}
            }
            CalibrationScreen(
                modifier = modifier,
                registerShutterTrigger = registerShutterTrigger,
                onCalibrated = { screen = AppScreen.Playing }
            )
        }
        is AppScreen.Playing -> {
            DisposableEffect(Unit) {
                setKeepScreenOn(true)
                onDispose { setKeepScreenOn(false) }
            }
            PlayScreen(
                modifier = modifier,
                tts = tts,
                registerShutterTrigger = registerShutterTrigger,
                onGameEnded = { screen = AppScreen.Calibration }
            )
        }
    }
}

@Composable
fun CameraPermissionGate(modifier: Modifier = Modifier, content: @Composable (Modifier) -> Unit) {
    val context = LocalContext.current
    var hasPermission by remember {
        mutableStateOf(
            ContextCompat.checkSelfPermission(context, Manifest.permission.CAMERA) ==
                PackageManager.PERMISSION_GRANTED
        )
    }
    val launcher = rememberLauncherForActivityResult(
        ActivityResultContracts.RequestPermission()
    ) { granted -> hasPermission = granted }

    if (hasPermission) {
        content(modifier.fillMaxSize())
    } else {
        Box(modifier = modifier.fillMaxSize()) {
            Column(
                modifier = Modifier.align(Alignment.Center),
                horizontalAlignment = Alignment.CenterHorizontally
            ) {
                Text(text = "撮影にはカメラの権限が必要です")
                Button(onClick = { launcher.launch(Manifest.permission.CAMERA) }) {
                    Text("カメラ権限を許可する")
                }
            }
        }
    }
}
