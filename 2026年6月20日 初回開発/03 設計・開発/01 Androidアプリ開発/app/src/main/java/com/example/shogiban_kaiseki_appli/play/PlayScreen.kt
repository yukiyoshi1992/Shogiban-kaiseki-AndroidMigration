package com.example.shogiban_kaiseki_appli.play

import android.content.Context
import android.media.AudioManager
import android.media.ToneGenerator
import android.speech.tts.TextToSpeech
import androidx.camera.core.CameraSelector
import androidx.camera.core.ImageCapture
import androidx.camera.core.ImageCaptureException
import androidx.camera.core.Preview
import androidx.camera.lifecycle.ProcessCameraProvider
import androidx.camera.view.PreviewView
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.material3.Button
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalLifecycleOwner
import androidx.compose.ui.unit.dp
import androidx.compose.ui.viewinterop.AndroidView
import androidx.core.content.ContextCompat
import com.example.shogiban_kaiseki_appli.camera.rememberShutterSound
import com.example.shogiban_kaiseki_appli.network.RetrofitClient
import kotlinx.coroutines.launch
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.MultipartBody
import okhttp3.RequestBody.Companion.asRequestBody
import java.io.File
import java.text.SimpleDateFormat
import java.util.Locale

private sealed class MoveResult {
    data class Move(val speechText: String, val moveCount: Int) : MoveResult()
    object NoChange : MoveResult()
    data class Error(val message: String) : MoveResult()
}

/**
 * 対局画面：カメラプレビュー常時表示、シャッター（オンスクリーンボタン or Bluetoothシャッター=
 * 音量キー、MainActivity側でフックしてregisterShutterTrigger経由でここに伝える）ごとに
 * /move を呼び指し手判定。成功時はTTSで読み上げ、失敗時は1回だけ自動で撮り直し、それでも
 * 失敗ならエラー音を鳴らして諦める（アプリ要件のエラー時方針）。
 */
@Composable
fun PlayScreen(
    modifier: Modifier = Modifier,
    tts: TextToSpeech?,
    registerShutterTrigger: (() -> Unit) -> Unit,
    onGameEnded: () -> Unit
) {
    val context = LocalContext.current
    val lifecycleOwner = LocalLifecycleOwner.current
    val coroutineScope = rememberCoroutineScope()

    var imageCapture by remember { mutableStateOf<ImageCapture?>(null) }
    var isBusy by remember { mutableStateOf(false) }
    var statusMessage by remember { mutableStateOf("対局中。シャッターを押すと1手撮影します。") }
    var moveCount by remember { mutableStateOf(0) }
    val playShutterSound = rememberShutterSound()

    fun captureOnce(onDone: (MoveResult) -> Unit) {
        val capture = imageCapture
        if (capture == null) {
            onDone(MoveResult.Error("カメラ未準備"))
            return
        }
        playShutterSound()
        val file = createTempPhotoFile(context, "move")
        val outputOptions = ImageCapture.OutputFileOptions.Builder(file).build()
        capture.takePicture(
            outputOptions,
            ContextCompat.getMainExecutor(context),
            object : ImageCapture.OnImageSavedCallback {
                override fun onImageSaved(output: ImageCapture.OutputFileResults) {
                    coroutineScope.launch { onDone(sendMove(file)) }
                }
                override fun onError(exception: ImageCaptureException) {
                    onDone(MoveResult.Error(exception.message ?: "撮影失敗"))
                }
            }
        )
    }

    fun performShutter(isRetry: Boolean = false) {
        if (isBusy) return
        isBusy = true
        statusMessage = if (isRetry) "もう一度撮影しています..." else "撮影中..."
        captureOnce { result ->
            when (result) {
                is MoveResult.Move -> {
                    moveCount = result.moveCount
                    statusMessage = "${result.moveCount}手目: ${result.speechText}"
                    tts?.speak(result.speechText, TextToSpeech.QUEUE_ADD, null, null)
                    isBusy = false
                }
                is MoveResult.NoChange -> {
                    statusMessage = "変化なし（駒のずれ・照明変化と判断、手は記録していません）"
                    isBusy = false
                }
                is MoveResult.Error -> {
                    if (!isRetry) {
                        tts?.speak("もう一度撮影してください", TextToSpeech.QUEUE_ADD, null, null)
                        isBusy = false
                        performShutter(isRetry = true)
                    } else {
                        playErrorTone()
                        statusMessage = "認識エラー: ${result.message}（もう一度シャッターを押してください）"
                        isBusy = false
                    }
                }
            }
        }
    }

    LaunchedEffect(Unit) {
        registerShutterTrigger { performShutter() }
    }

    Box(modifier = modifier.fillMaxSize()) {
        AndroidView(
            modifier = Modifier.fillMaxSize(),
            factory = { ctx ->
                val previewView = PreviewView(ctx)
                val cameraProviderFuture = ProcessCameraProvider.getInstance(ctx)
                cameraProviderFuture.addListener({
                    val cameraProvider = cameraProviderFuture.get()
                    val preview = Preview.Builder().build().also {
                        it.setSurfaceProvider(previewView.surfaceProvider)
                    }
                    val capture = ImageCapture.Builder().build()
                    imageCapture = capture
                    cameraProvider.unbindAll()
                    cameraProvider.bindToLifecycle(
                        lifecycleOwner, CameraSelector.DEFAULT_BACK_CAMERA, preview, capture
                    )
                }, ContextCompat.getMainExecutor(ctx))
                previewView
            }
        )
        Column(
            modifier = Modifier.fillMaxWidth().align(Alignment.BottomCenter)
                .background(MaterialTheme.colorScheme.surface.copy(alpha = 0.85f)).padding(16.dp),
            horizontalAlignment = Alignment.CenterHorizontally
        ) {
            Text(text = statusMessage)
            Row(
                modifier = Modifier.fillMaxWidth().padding(top = 12.dp),
                horizontalArrangement = Arrangement.SpaceEvenly
            ) {
                Button(
                    modifier = Modifier.size(64.dp),
                    shape = CircleShape,
                    enabled = !isBusy,
                    onClick = { performShutter() }
                ) {}
                Button(onClick = {
                    coroutineScope.launch { endGame(onGameEnded) }
                }) { Text("終局") }
            }
        }
    }
}

private fun createTempPhotoFile(context: Context, prefix: String): File {
    val timestamp = SimpleDateFormat("yyyyMMdd_HHmmss_SSS", Locale.US).format(java.util.Date())
    return File(context.cacheDir, "${prefix}_$timestamp.jpg")
}

private suspend fun sendMove(file: File): MoveResult {
    return try {
        val requestBody = file.asRequestBody("image/jpeg".toMediaType())
        val part = MultipartBody.Part.createFormData("file", file.name, requestBody)
        val response = RetrofitClient.shogiApiService.move(part)
        val body = response.body()
        when {
            !response.isSuccessful || body == null -> MoveResult.Error("HTTP ${response.code()}")
            body.status == "move" -> MoveResult.Move(body.speech_text ?: "", body.move_count ?: 0)
            body.status == "nochange" -> MoveResult.NoChange
            else -> MoveResult.Error(body.detail?.toString() ?: "認識できませんでした")
        }
    } catch (e: Exception) {
        MoveResult.Error(e.message ?: "通信エラー")
    }
}

private suspend fun endGame(onGameEnded: () -> Unit) {
    try {
        val response = RetrofitClient.shogiApiService.gameEnd()
        if (response.isSuccessful) onGameEnded()
    } catch (e: Exception) {
        // 終局通知が失敗してもアプリ側は対局画面に留まる（再度「終局」を押せば再試行できる）
    }
}

private fun playErrorTone() {
    val tg = ToneGenerator(AudioManager.STREAM_NOTIFICATION, ToneGenerator.MAX_VOLUME)
    tg.startTone(ToneGenerator.TONE_PROP_BEEP, 400)
}
