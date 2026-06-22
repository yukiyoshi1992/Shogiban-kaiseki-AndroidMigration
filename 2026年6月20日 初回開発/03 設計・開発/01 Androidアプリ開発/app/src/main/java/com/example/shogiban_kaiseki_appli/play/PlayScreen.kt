package com.example.shogiban_kaiseki_appli.play

import android.content.Context
import android.media.AudioManager
import android.media.ToneGenerator
import android.speech.tts.TextToSpeech
import androidx.camera.core.CameraSelector
import androidx.camera.core.ImageCapture
import androidx.camera.core.ImageCaptureException
import androidx.camera.core.Preview
import androidx.camera.core.resolutionselector.AspectRatioStrategy
import androidx.camera.core.resolutionselector.ResolutionSelector
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
import androidx.compose.runtime.rememberUpdatedState
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
import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.launch
import kotlinx.coroutines.suspendCancellableCoroutine
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
 * /move を呼び指し手判定。成功時はTTSで読み上げ、認識エラー時はエラー音を鳴らして分析を
 * 中断する（撮影のし直しは行わない——2026-06-22、UAT2回目課題②によりこの方針に変更。
 * 旧仕様の「自動で1回だけ撮り直す」は過剰な追加機能だったとユーザー判断、撤回済み）。
 *
 * 2026-06-22、初回UAT指摘により改修：①連続して撮影すると前の写真の処理待ちで次のシャッターが
 * 無視され「固まる」（isBusy中はpress自体を無視していたため、押した分の手が記録されずに
 * 消えていた）→ 撮影自体（カメラのシャッター）は常に即座に行い、サーバへの送信・判定は
 * Channelで1件ずつ順番に処理するキュー方式に変更。シャッターを連打しても押した回数分の
 * 写真は必ず撮られ、順番に処理される（処理中も「あと何件待ち」を表示）。
 * ②「対局中止」ボタンを追加（後述）。
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
    val currentImageCapture = rememberUpdatedState(imageCapture)
    var pendingCount by remember { mutableStateOf(0) }
    var isProcessing by remember { mutableStateOf(false) }
    var statusMessage by remember { mutableStateOf("対局中。シャッターを押すと1手撮影します。") }
    var moveCount by remember { mutableStateOf(0) }
    // 2026-06-22、2回目UAT課題④：読み上げON/OFF切り替え。OFF中もKIF記録・画面表示は
    // 通常通り続ける（読み上げ（TTSのspeak呼び出し）だけを止める）。
    var ttsEnabled by remember { mutableStateOf(true) }
    val playShutterSound = rememberShutterSound()
    val captureQueue = remember { Channel<File>(Channel.UNLIMITED) }

    // カメラのシャッター（CameraXのtakePicture）を待つだけのsuspend関数。
    // 撮影自体はネットワークを待たず即座に終わるので、ここではキューに積まない
    // （積むのはファイルが出来上がった後）。
    suspend fun captureToFile(prefix: String): File? {
        val capture = currentImageCapture.value ?: return null
        val file = createTempPhotoFile(context, prefix)
        val outputOptions = ImageCapture.OutputFileOptions.Builder(file).build()
        val ok = suspendCancellableCoroutine<Boolean> { cont ->
            capture.takePicture(
                outputOptions,
                ContextCompat.getMainExecutor(context),
                object : ImageCapture.OnImageSavedCallback {
                    override fun onImageSaved(output: ImageCapture.OutputFileResults) {
                        if (cont.isActive) cont.resume(true) {}
                    }
                    override fun onError(exception: ImageCaptureException) {
                        if (cont.isActive) cont.resume(false) {}
                    }
                }
            )
        }
        return if (ok) file else null
    }

    // キューから1件取り出して処理する。
    // 2026-06-22、UAT2回目課題②により撮り直し（リトライ）を全廃：認識エラー時は即座に
    // エラー表示・エラー音で分析を中断するだけにする（撮影のし直しは不要、というユーザーの
    // 明示の指示。前PJ・アプリ要件にあった「自動で1回だけ撮り直す」方針は、今回の指摘により
    // 過剰な追加機能だったと判断され撤回——必要になれば改めて開発する）。
    //
    // この撤回は同時に②で報告された「連射すると処理が混乱する」不具合の真因も解消する：
    // 旧実装はエラー時、キューの先頭処理中にその場で新しい撮影を割り込ませて即座に処理していた
    // （`captureToFile`→再帰`processFile`）。これはキューに後続の写真（連射で既に積まれている分）
    // が残っている状態では、本来の順序より先に「今の」実際の盤面を撮ってしまい、キュー内の
    // 古い写真との比較がズレて余計なエラーを誘発する一因になっていた（順序が保証された
    // キュー処理の前提を、割り込みの再撮影が破っていた）。撮り直し自体をやめたことで、
    // キューは常に「撮影された順番のまま、1件ずつ」処理される構造になり、この混乱は起きない。
    suspend fun processFile(file: File) {
        statusMessage = if (pendingCount > 0) {
            "処理中...（あと${pendingCount}件待ち）"
        } else {
            "処理中..."
        }
        when (val result = sendMove(file)) {
            is MoveResult.Move -> {
                moveCount = result.moveCount
                statusMessage = "${result.moveCount}手目: ${result.speechText}"
                if (ttsEnabled) tts?.speak(result.speechText, TextToSpeech.QUEUE_ADD, null, null)
            }
            is MoveResult.NoChange -> {
                statusMessage = "変化なし（駒のずれ・照明変化と判断、手は記録していません）"
            }
            is MoveResult.Error -> {
                playErrorTone()
                statusMessage = "認識エラー: ${result.message}（分析を中断しました。もう一度シャッターを押してください）"
            }
        }
    }

    // キューを順番に処理する常駐コルーチン。画面が表示されている間ずっと1つだけ動く。
    LaunchedEffect(captureQueue) {
        for (file in captureQueue) {
            isProcessing = true
            processFile(file)
            pendingCount = (pendingCount - 1).coerceAtLeast(0)
            isProcessing = false
        }
    }

    fun triggerCapture() {
        coroutineScope.launch {
            playShutterSound()
            val file = captureToFile("move")
            if (file == null) {
                statusMessage = "カメラ未準備のため撮影できませんでした"
                return@launch
            }
            pendingCount += 1
            captureQueue.trySend(file)
        }
    }

    LaunchedEffect(Unit) {
        registerShutterTrigger { triggerCapture() }
    }

    Box(modifier = modifier.fillMaxSize()) {
        AndroidView(
            modifier = Modifier.fillMaxSize(),
            factory = { ctx ->
                val previewView = PreviewView(ctx).apply {
                    // デフォルトのFILL_CENTERはプレビューを拡大表示するため、実際の撮影範囲より
                    // 狭く見える（ズームされて見える）。FIT_CENTERで実際の撮影範囲どおりに表示する。
                    scaleType = PreviewView.ScaleType.FIT_CENTER
                }
                val cameraProviderFuture = ProcessCameraProvider.getInstance(ctx)
                cameraProviderFuture.addListener({
                    val cameraProvider = cameraProviderFuture.get()
                    // 前PJのカメラ仕様（16:9、3840x2160）に合わせる。CalibrationScreenと同じ理由
                    // （2026-06-21）。
                    val resolutionSelector = ResolutionSelector.Builder()
                        .setAspectRatioStrategy(AspectRatioStrategy.RATIO_16_9_FALLBACK_AUTO_STRATEGY)
                        .build()
                    val preview = Preview.Builder()
                        .setResolutionSelector(resolutionSelector)
                        .build().also {
                            it.setSurfaceProvider(previewView.surfaceProvider)
                        }
                    val capture = ImageCapture.Builder()
                        .setResolutionSelector(resolutionSelector)
                        .build()
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
                    onClick = { triggerCapture() }
                ) {}
                Button(onClick = {
                    coroutineScope.launch { endGame(onGameEnded) }
                }) { Text("終局") }
                // 2026-06-22、UAT課題①：テスト時に対局を中断して最初からやり直したい場面が多く
                // 「終局」しかないと使いづらいという指摘への対応。「終局」は/move側の記録が
                // 正規に終わったことを意味するのに対し、「中止」はテスト的な強制リセットなので
                // サーバへの通知が失敗しても（ネットワーク不調等）必ず画面は戻す——でないと
                // 「中止すら効かない」というさらに使いづらい状態になってしまうため。
                Button(onClick = {
                    coroutineScope.launch { abortGame(onGameEnded) }
                }) { Text("中止") }
                Button(onClick = { ttsEnabled = !ttsEnabled }) {
                    Text(if (ttsEnabled) "読み上げON" else "読み上げOFF")
                }
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

private suspend fun abortGame(onGameEnded: () -> Unit) {
    try {
        RetrofitClient.shogiApiService.gameAbort()
    } catch (e: Exception) {
        // 中止はテスト時の強制リセットなので、通知が失敗しても画面は必ず戻す（下のonGameEnded）。
    }
    onGameEnded()
}

private fun playErrorTone() {
    val tg = ToneGenerator(AudioManager.STREAM_NOTIFICATION, ToneGenerator.MAX_VOLUME)
    tg.startTone(ToneGenerator.TONE_PROP_BEEP, 400)
}
