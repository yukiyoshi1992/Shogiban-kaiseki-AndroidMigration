package com.example.shogiban_kaiseki_appli.play

import android.content.Context
import android.media.AudioManager
import android.media.ToneGenerator
import android.speech.tts.TextToSpeech
import android.widget.Toast
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
import kotlinx.coroutines.delay
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
    // サーバが実際にclassify_frameを実行した結果"error"を返したケース（応答は正常に届いている）。
    // 前PJ方針通り対局を自動中止する対象はこれだけ——moveCountはサーバが返した
    // 「ここまでに記録済みの手数」。
    data class RecognitionError(val message: String, val moveCount: Int? = null) : MoveResult()
    // 2026-06-22、UAT報告（撮影すると409が出る）の原因調査で追加：上記とは区別する。
    // サーバに届く前/応答が返る前に失敗したケース（タイムアウト・接続不可・HTTPエラー等）。
    // これは「認識した結果がエラーだった」のではなく「認識できたかどうかすら分からない」ので、
    // 区別せずRecognitionErrorと同様に即座に自動中止していたのが今回の不具合の真因：その
    // 「中止」自体も通信（/game/abort）なので、ネットワークがまだ不調だとそれも届かず、
    // クライアントだけが対局を終えた扱いになりサーバはplayingのまま残ってしまい、その後の
    // 撮影が全部409 Conflictになっていた。
    // 2026-06-22、ユーザー指摘により再修正：通信エラー発生時点で対局が既に進んでいる
    // （駒が動いている）可能性があるため、即座に諦めず同じ写真で数回バックグラウンド再試行する
    // （processFile参照）。それでも届かない場合のみ「記録不能」と判断し対局を自動中止する。
    data class NetworkError(val message: String) : MoveResult()
}

/** 通信エラー時の再試行回数・間隔。WiFiの一時的な切断を乗り切れる程度に短く設定。 */
private const val NETWORK_RETRY_MAX = 3
private const val NETWORK_RETRY_DELAY_MS = 1500L

/**
 * 対局画面：カメラプレビュー常時表示、シャッター（オンスクリーンボタン or Bluetoothシャッター=
 * 音量キー、MainActivity側でフックしてregisterShutterTrigger経由でここに伝える）ごとに
 * /move を呼び指し手判定。成功時はTTSで読み上げ、認識エラー時はエラー音＋ポップアップで理由を
 * 示し、前PJ`app_streamlit.py`の方針通り対局を自動中止する（2026-06-22、UAT2回目課題⑤。
 * 撮り直しを促すだけで止まる旧仕様は撤回済み——課題②で「撮影のし直しは不要」と確定済み）。
 *
 * 2026-06-22、初回UAT指摘により改修：①連続して撮影すると前の写真の処理待ちで次のシャッターが
 * 無視され「固まる」（isBusy中はpress自体を無視していたため、押した分の手が記録されずに
 * 消えていた）→ 撮影自体（カメラのシャッター）は常に即座に行い、サーバへの送信・判定は
 * Channelで1件ずつ順番に処理するキュー方式に変更。シャッターを連打しても押した回数分の
 * 写真は必ず撮られ、順番に処理される（処理中も「あと何件待ち」を表示）。
 * ②「対局中止」ボタンを追加（後述）。
 * 2026-06-22、2回目UAT課題⑤の調査で、シャッター発行自体（カメラのtakePicture呼び出し）も
 * 連射時に並行実行されており完了順が入れ替わる余地があったため、シャッター要求も直列キュー化。
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

    // キューから1件取り出して処理する。戻り値true＝認識エラーで対局を自動中止したので、
    // 残りのキューはもう処理しない（呼び出し側でループを止める）。
    //
    // 2026-06-22、UAT2回目課題②により撮り直し（リトライ）を全廃：認識エラー時に「その場で
    // 新しい撮影を割り込ませて先に処理する」旧実装が、連射でキューに後続の写真が残っている
    // 状態だと順序を乱し、それ自体が誤エラーの一因になっていた（キュー処理の「撮影順を保つ」
    // 前提を、割り込みの再撮影が破っていたため）。撮り直し自体をやめたことでこの混乱は解消。
    //
    // 2026-06-22、UAT2回目課題⑤：認識エラー時、前PJ`app_streamlit.py`の方針
    // （画面要件No.6「認識エラーで続行不能→対局中止と同じ動作を自動実行」、
    // 誤ったKIFを生成するより諦める）を踏襲し、撮り直しを促すだけで止まらず、対局を
    // 自動的に中止する。中止理由（サーバの判定詳細＋ここまでの手数）はトースト（ポップアップ）
    // で表示する——画面がこの直後に対局画面から抜けてしまうため、画面内のテキストでは
    // ユーザーが読み取れない可能性があるため。KIFは（中止ボタンと同じ`abortGame`経由なので）
    // 削除されず、記録済みの手数まではそのまま残る。
    //
    // 2026-06-22、上記の自動中止後に実機で再現した不具合への対応：自動中止はサーバが実際に
    // classify_frameを動かして"error"を返した場合（MoveResult.RecognitionError）のみに限定し、
    // 単なる通信エラー（タイムアウト・接続不可・409等、MoveResult.NetworkError）では区別する。
    // 原因：通信が不調な時に/moveが失敗すると、当初は区別なく即座に対局を自動中止しに行って
    // いたが、その「中止」自体も通信（/game/abort）なので、ネットワークがまだ不調だとそれも
    // 届かない。するとクライアントは（abortGameがonGameEndedを無条件で呼ぶため）対局を終えた
    // 扱いになり画面が戻るが、サーバ側のセッションはplayingのまま残ってしまい、以後の撮影が
    // 全部409 Conflictになる、という実機報告と一致する不具合があった。
    //
    // 2026-06-22、ユーザー指摘により再修正：通信エラーを検知した時点では対局がどこまで進んだか
    // 不明（駒が動いた可能性がある）ため、単に「もう一度シャッターを押してください」と促すだけ
    // では1手取りこぼす恐れがある。同じ写真（撮り直し不要）で数回バックグラウンド再試行し、
    // それでも届かない場合のみ「記録不能」と判断して対局を自動中止する（RecognitionErrorと
    // 同様にエラー音＋ポップアップで明示——通信エラーで終わったことに気づけないと困るため）。
    suspend fun processFile(file: File): Boolean {
        statusMessage = if (pendingCount > 0) {
            "処理中...（あと${pendingCount}件待ち）"
        } else {
            "処理中..."
        }
        var result = sendMove(file)
        var retryCount = 0
        while (result is MoveResult.NetworkError && retryCount < NETWORK_RETRY_MAX) {
            retryCount++
            statusMessage = "通信エラー、再試行中...(${retryCount}/${NETWORK_RETRY_MAX})"
            delay(NETWORK_RETRY_DELAY_MS)
            result = sendMove(file)
        }
        when (val finalResult = result) {
            is MoveResult.Move -> {
                moveCount = finalResult.moveCount
                statusMessage = "${finalResult.moveCount}手目: ${finalResult.speechText}"
                if (ttsEnabled) tts?.speak(finalResult.speechText, TextToSpeech.QUEUE_ADD, null, null)
                return false
            }
            is MoveResult.NoChange -> {
                statusMessage = "変化なし（駒のずれ・照明変化と判断、手は記録していません）"
                return false
            }
            is MoveResult.RecognitionError -> {
                playErrorTone()
                val recordedCount = finalResult.moveCount ?: moveCount
                val reasonMsg = "認識エラーのため対局を中止しました（${recordedCount}手目まで記録）\n詳細: ${finalResult.message}"
                statusMessage = reasonMsg
                Toast.makeText(context, reasonMsg, Toast.LENGTH_LONG).show()
                abortGame(onGameEnded)
                return true
            }
            is MoveResult.NetworkError -> {
                // 再試行しても届かなかった＝記録不能。RecognitionErrorと同様に対局を自動中止する。
                playErrorTone()
                val reasonMsg = "通信エラーのため対局を中止しました（${moveCount}手目まで記録）\n詳細: ${finalResult.message}"
                statusMessage = reasonMsg
                Toast.makeText(context, reasonMsg, Toast.LENGTH_LONG).show()
                abortGame(onGameEnded)
                return true
            }
        }
    }

    // キューを順番に処理する常駐コルーチン。画面が表示されている間ずっと1つだけ動く。
    // 認識エラーで対局が自動中止された場合はそこで止め、キューに残っている分（連射で
    // 既に積まれていた古い写真）は処理しない（対局がもう存在しないため）。
    LaunchedEffect(captureQueue) {
        for (file in captureQueue) {
            isProcessing = true
            val aborted = processFile(file)
            pendingCount = (pendingCount - 1).coerceAtLeast(0)
            isProcessing = false
            if (aborted) {
                pendingCount = 0
                break
            }
        }
    }

    // シャッター（連打含む）を順番に処理する常駐コルーチン。
    // 2026-06-22、UAT2回目課題⑤の調査で判明：以前はシャッター1回ごとに新しいコルーチンを
    // 独立起動していたため（`coroutineScope.launch { ...captureToFile...captureQueue.trySend... }`）、
    // 連射時に複数の撮影が並行して走り、CameraXへのtakePicture発行順・完了順が押した順と
    // 入れ替わる余地があった（撮影完了後にキューへ送る設計なので、後で押した分が先に完了すれば
    // 先にキューに入り、結果的にサーバ側の処理順が実際の手順と食い違う）。これは「撮影の
    // タイミングに誤りがある」場合の一種で、上記⑤の自動中止で必ず救済できるが、そもそも
    // 起きないようにする方が良いため、シャッター要求自体を1件ずつ直列処理するキューに変更した
    // （カメラ撮影自体は数百msで終わるので、直列化してもシャッターが固まる印象には繋がらない
    // ——固まって見えた初回UATの不具合は、ネットワーク/認識処理待ちが原因で、これとは別）。
    val triggerQueue = remember { Channel<Unit>(Channel.UNLIMITED) }
    LaunchedEffect(triggerQueue) {
        for (unit in triggerQueue) {
            playShutterSound()
            val file = captureToFile("move")
            if (file == null) {
                statusMessage = "カメラ未準備のため撮影できませんでした"
                continue
            }
            pendingCount += 1
            captureQueue.trySend(file)
        }
    }

    fun triggerCapture() {
        triggerQueue.trySend(Unit)
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
            // HTTPレベルで失敗（409=状態不整合、その他5xx等含む）はサーバが実際にclassify_frameを
            // 動かせてすらいないので、認識エラーとして対局を中止する対象ではない（NetworkError）。
            !response.isSuccessful || body == null -> MoveResult.NetworkError("HTTP ${response.code()}")
            body.status == "move" -> MoveResult.Move(body.speech_text ?: "", body.move_count ?: 0)
            body.status == "nochange" -> MoveResult.NoChange
            else -> MoveResult.RecognitionError(body.detail?.toString() ?: "認識できませんでした", body.move_count)
        }
    } catch (e: Exception) {
        MoveResult.NetworkError(e.message ?: "通信エラー")
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
