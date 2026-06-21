package com.example.shogiban_kaiseki_appli.calibration

import android.content.Context
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import androidx.camera.core.CameraSelector
import androidx.camera.core.ImageCapture
import androidx.camera.core.ImageCaptureException
import androidx.camera.core.Preview
import androidx.camera.lifecycle.ProcessCameraProvider
import androidx.camera.view.PreviewView
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.Image
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.BoxWithConstraints
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
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
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.asImageBitmap
import androidx.compose.ui.input.pointer.awaitEachGesture
import androidx.compose.ui.input.pointer.awaitFirstDown
import androidx.compose.ui.input.pointer.changedToUp
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.layout.onSizeChanged
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.platform.LocalLifecycleOwner
import androidx.compose.ui.unit.IntOffset
import androidx.compose.ui.unit.IntSize
import androidx.compose.ui.unit.dp
import androidx.compose.ui.viewinterop.AndroidView
import androidx.core.content.ContextCompat
import com.example.shogiban_kaiseki_appli.camera.rememberShutterSound
import com.example.shogiban_kaiseki_appli.network.RetrofitClient
import com.google.gson.Gson
import kotlinx.coroutines.launch
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.MultipartBody
import okhttp3.RequestBody.Companion.asRequestBody
import okhttp3.RequestBody.Companion.toRequestBody
import java.io.File
import java.text.SimpleDateFormat
import java.util.Locale
import kotlin.math.roundToInt

private enum class CalibPhase { CAMERA, AUTO_SUBMITTING, TAPPING, SUBMITTING, MISMATCH, ERROR }

/** decodeForDisplay()が返す元画像（撮影フルサイズ）のピクセル寸法。タップ座標をこの基準に変換して送信する。 */
private data class PixelSize(val width: Int, val height: Int)

/**
 * キャリブレーション画面：①撮影 → ②まず前PJと同じ赤丸4個の自動検出をサーバに依頼
 * → 検出できなければ③盤の四隅4点タップ（ドラッグ＋拡大ルーペで微調整可能）→サーバへ送信
 * → ④認識結果が将棋の初期配置と一致するかをサーバが自動判定し、一致すればそのまま対局開始。
 * 不一致なら①からやり直し（個別マス補正はしない、確定済み方針）。
 * 2026-06-21、実機テストでのユーザー指摘により2点変更：
 *   - 「人間が9x9を目視確認してOKを押す」フローから「サーバが自動判定する」フローに変更。
 *   - 手動4隅タップを毎回先に行う方式から、前PJと同じ「まず赤丸自動検出、失敗時のみ人間が
 *     タップ」の二段構成に変更（赤丸検出・盤面分析ロジック自体は前PJのものをそのまま移植、
 *     変更していない）。
 */
@Composable
fun CalibrationScreen(
    modifier: Modifier = Modifier,
    registerShutterTrigger: (() -> Unit) -> Unit,
    onCalibrated: () -> Unit
) {
    val coroutineScope = rememberCoroutineScope()

    var phase by remember { mutableStateOf(CalibPhase.CAMERA) }
    var imageCapture by remember { mutableStateOf<ImageCapture?>(null) }
    var capturedFile by remember { mutableStateOf<File?>(null) }
    var capturedBitmap by remember { mutableStateOf<Bitmap?>(null) }
    var originalSize by remember { mutableStateOf(PixelSize(0, 0)) }
    var tapPoints by remember { mutableStateOf(listOf<Offset>()) }
    var mismatchCount by remember { mutableStateOf(0) }
    var errorMessage by remember { mutableStateOf("") }

    fun resetToCamera() {
        phase = CalibPhase.CAMERA
        capturedFile = null
        capturedBitmap = null
        tapPoints = emptyList()
    }

    fun handleCalibrationResult(file: File, points: List<List<Double>>?) {
        coroutineScope.launch {
            submitCalibration(file, points,
                onMatch = {
                    coroutineScope.launch {
                        confirmCalibration(
                            onSuccess = { onCalibrated() },
                            onError = { msg -> errorMessage = msg; phase = CalibPhase.ERROR }
                        )
                    }
                },
                onMismatch = { count -> mismatchCount = count; phase = CalibPhase.MISMATCH },
                onCalibrationFailed = { phase = CalibPhase.TAPPING },
                onError = { msg -> errorMessage = msg; phase = CalibPhase.ERROR }
            )
        }
    }

    when (phase) {
        CalibPhase.CAMERA -> CameraCaptureStep(
            modifier = modifier,
            imageCapture = imageCapture,
            onImageCaptureReady = { imageCapture = it },
            registerShutterTrigger = registerShutterTrigger,
            onCaptured = { file ->
                val (bitmap, size) = decodeForDisplay(file, maxWidthPx = 1600)
                capturedFile = file
                capturedBitmap = bitmap
                originalSize = size
                tapPoints = emptyList()
                phase = CalibPhase.AUTO_SUBMITTING
                handleCalibrationResult(file, points = null)
            }
        )

        CalibPhase.AUTO_SUBMITTING -> Box(modifier = modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
            Text("赤丸を自動検出中...")
        }

        CalibPhase.TAPPING -> TappingStep(
            modifier = modifier,
            bitmap = capturedBitmap!!,
            points = tapPoints,
            onPointsChanged = { tapPoints = it },
            onRetake = { resetToCamera() },
            onSubmit = { boxSizePx ->
                phase = CalibPhase.SUBMITTING
                val scaled = tapPoints.map { p ->
                    listOf(
                        (p.x / boxSizePx.width * originalSize.width).toDouble(),
                        (p.y / boxSizePx.height * originalSize.height).toDouble()
                    )
                }
                handleCalibrationResult(capturedFile!!, points = scaled)
            }
        )

        CalibPhase.SUBMITTING -> Box(modifier = modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
            Text("サーバで判定中...")
        }

        CalibPhase.MISMATCH -> Box(modifier = modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
            Column(horizontalAlignment = Alignment.CenterHorizontally) {
                Text(text = "認識失敗：初期配置と${mismatchCount}箇所異なります")
                Text(text = "盤の四隅タップがズレている可能性があります。撮影からやり直してください")
                Button(onClick = { resetToCamera() }) { Text("撮影からやり直す") }
            }
        }

        CalibPhase.ERROR -> Box(modifier = modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
            Column(horizontalAlignment = Alignment.CenterHorizontally) {
                Text(text = "エラー: $errorMessage")
                Button(onClick = { resetToCamera() }) { Text("撮影からやり直す") }
            }
        }
    }
}

@Composable
private fun CameraCaptureStep(
    modifier: Modifier,
    imageCapture: ImageCapture?,
    onImageCaptureReady: (ImageCapture) -> Unit,
    registerShutterTrigger: (() -> Unit) -> Unit,
    onCaptured: (File) -> Unit
) {
    val context = LocalContext.current
    val lifecycleOwner = LocalLifecycleOwner.current
    val playShutterSound = rememberShutterSound()

    fun capture() {
        val capture = imageCapture ?: return
        playShutterSound()
        val file = createTempPhotoFile(context, "calib")
        val outputOptions = ImageCapture.OutputFileOptions.Builder(file).build()
        capture.takePicture(
            outputOptions,
            ContextCompat.getMainExecutor(context),
            object : ImageCapture.OnImageSavedCallback {
                override fun onImageSaved(output: ImageCapture.OutputFileResults) {
                    onCaptured(file)
                }
                override fun onError(exception: ImageCaptureException) {}
            }
        )
    }

    LaunchedEffect(Unit) {
        registerShutterTrigger { capture() }
    }

    Box(modifier = modifier.fillMaxSize()) {
        AndroidView(
            modifier = Modifier.fillMaxSize(),
            factory = { ctx ->
                val previewView = PreviewView(ctx).apply {
                    // デフォルトのFILL_CENTERだとプレビューが表示枠いっぱいに拡大され、
                    // 実際の撮影範囲より狭く（ズームされたように）見えてしまう。
                    // FIT_CENTERにして撮影される範囲をそのまま（レターボックス込みで）表示する。
                    scaleType = PreviewView.ScaleType.FIT_CENTER
                }
                val cameraProviderFuture = ProcessCameraProvider.getInstance(ctx)
                cameraProviderFuture.addListener({
                    val cameraProvider = cameraProviderFuture.get()
                    val preview = Preview.Builder().build().also {
                        it.setSurfaceProvider(previewView.surfaceProvider)
                    }
                    val capture = ImageCapture.Builder().build()
                    onImageCaptureReady(capture)
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
                .background(MaterialTheme.colorScheme.surface.copy(alpha = 0.85f)).padding(24.dp),
            horizontalAlignment = Alignment.CenterHorizontally
        ) {
            Text("盤全体が入るように撮影してください（音量キーでも撮影できます）")
            Button(
                modifier = Modifier.size(72.dp),
                shape = CircleShape,
                onClick = { capture() }
            ) {}
        }
    }
}

/**
 * 盤の四隅タップ用ステップ。タップ＝指を置いた瞬間にその位置へ点を置き、指を離すまでドラッグで
 * 微調整できる。ドラッグ中は指の周辺を拡大した「ルーペ」を表示し、細かい位置決めをしやすくする
 * （2026-06-21、「タップの難易度が高すぎる」というユーザー指摘への対応）。
 * 既存の点の近く（hitRadius以内）でドラッグを始めればその点を再調整、それ以外の場所なら
 * 新しい点を追加する（4点に達したら無視）。
 */
@Composable
private fun TappingStep(
    modifier: Modifier,
    bitmap: Bitmap,
    points: List<Offset>,
    onPointsChanged: (List<Offset>) -> Unit,
    onRetake: () -> Unit,
    onSubmit: (boxSizePx: PixelSize) -> Unit
) {
    val density = LocalDensity.current
    val aspect = bitmap.width.toFloat() / bitmap.height.toFloat()
    var measuredSize by remember { mutableStateOf(PixelSize(1, 1)) }
    var magnifierPosition by remember { mutableStateOf<Offset?>(null) }
    val hitRadiusPx = with(density) { 28.dp.toPx() }

    Column(modifier = modifier.fillMaxSize()) {
        Text(
            text = "盤の四隅（マス目の角、駒台は不要）を押さえてドラッグで微調整してください（${points.size}/4）",
            modifier = Modifier.padding(8.dp)
        )
        BoxWithConstraints(modifier = Modifier.fillMaxWidth().weight(1f)) {
            val boxHeightDp = maxWidth / aspect
            Box(
                modifier = Modifier
                    .fillMaxWidth()
                    .height(boxHeightDp)
                    .onSizeChanged { measuredSize = PixelSize(it.width, it.height) }
                    .pointerInput(points) {
                        awaitEachGesture {
                            val down = awaitFirstDown()
                            val startPos = down.position
                            val nearestIndex = points.indices.minByOrNull { i ->
                                (points[i] - startPos).getDistance()
                            }
                            val targetIndex = if (
                                nearestIndex != null &&
                                (points[nearestIndex] - startPos).getDistance() < hitRadiusPx
                            ) {
                                nearestIndex
                            } else if (points.size < 4) {
                                points.size
                            } else {
                                null
                            }
                            if (targetIndex == null) return@awaitEachGesture

                            var current = points.toMutableList()
                            if (targetIndex == current.size) current.add(startPos) else current[targetIndex] = startPos
                            onPointsChanged(current.toList())
                            magnifierPosition = startPos
                            down.consume()

                            while (true) {
                                val event = awaitPointerEvent()
                                val change = event.changes.firstOrNull { it.id == down.id } ?: break
                                if (change.changedToUp()) {
                                    magnifierPosition = null
                                    break
                                }
                                current = current.toMutableList()
                                current[targetIndex] = change.position
                                onPointsChanged(current.toList())
                                magnifierPosition = change.position
                                change.consume()
                            }
                        }
                    }
            ) {
                Image(
                    bitmap = bitmap.asImageBitmap(),
                    contentDescription = null,
                    modifier = Modifier.fillMaxSize()
                )
                Canvas(modifier = Modifier.fillMaxSize()) {
                    points.forEach { p -> drawCircle(color = Color.Red, radius = 12f, center = p) }

                    val magPos = magnifierPosition
                    if (magPos != null && measuredSize.width > 0) {
                        val scaleToBitmap = bitmap.width.toFloat() / measuredSize.width
                        val magnifierSizePx = 160.dp.toPx()
                        val cropBoxPx = 36.dp.toPx()
                        val cropBitmapPx = (cropBoxPx * 2 * scaleToBitmap).roundToInt().coerceAtLeast(1)

                        val centerBitmapX = magPos.x * scaleToBitmap
                        val centerBitmapY = magPos.y * scaleToBitmap
                        val srcX = (centerBitmapX - cropBitmapPx / 2f).roundToInt()
                            .coerceIn(0, (bitmap.width - cropBitmapPx).coerceAtLeast(0))
                        val srcY = (centerBitmapY - cropBitmapPx / 2f).roundToInt()
                            .coerceIn(0, (bitmap.height - cropBitmapPx).coerceAtLeast(0))

                        val gapPx = 24.dp.toPx()
                        var dstLeft = magPos.x - magnifierSizePx / 2f
                        var dstTop = magPos.y - magnifierSizePx - gapPx
                        if (dstTop < 0f) dstTop = magPos.y + gapPx
                        dstLeft = dstLeft.coerceIn(0f, (size.width - magnifierSizePx).coerceAtLeast(0f))
                        dstTop = dstTop.coerceIn(0f, (size.height - magnifierSizePx).coerceAtLeast(0f))

                        drawImage(
                            image = bitmap.asImageBitmap(),
                            srcOffset = IntOffset(srcX, srcY),
                            srcSize = IntSize(cropBitmapPx, cropBitmapPx),
                            dstOffset = IntOffset(dstLeft.roundToInt(), dstTop.roundToInt()),
                            dstSize = IntSize(magnifierSizePx.roundToInt(), magnifierSizePx.roundToInt())
                        )
                        drawRect(
                            color = Color.White,
                            topLeft = Offset(dstLeft, dstTop),
                            size = androidx.compose.ui.geometry.Size(magnifierSizePx, magnifierSizePx),
                            style = androidx.compose.ui.graphics.drawscope.Stroke(width = 4f)
                        )
                        val centerX = dstLeft + magnifierSizePx / 2f
                        val centerY = dstTop + magnifierSizePx / 2f
                        drawLine(Color.Red, Offset(centerX - 14f, centerY), Offset(centerX + 14f, centerY), strokeWidth = 3f)
                        drawLine(Color.Red, Offset(centerX, centerY - 14f), Offset(centerX, centerY + 14f), strokeWidth = 3f)
                    }
                }
            }
        }
        Row(
            modifier = Modifier.fillMaxWidth().padding(16.dp),
            horizontalArrangement = Arrangement.SpaceEvenly
        ) {
            Button(onClick = onRetake) { Text("撮り直す") }
            Button(onClick = { onPointsChanged(emptyList()) }, enabled = points.isNotEmpty()) { Text("タップをやり直す") }
            Button(enabled = points.size == 4, onClick = { onSubmit(measuredSize) }) { Text("送信") }
        }
    }
}

private fun createTempPhotoFile(context: Context, prefix: String): File {
    val timestamp = SimpleDateFormat("yyyyMMdd_HHmmss_SSS", Locale.US).format(java.util.Date())
    return File(context.cacheDir, "${prefix}_$timestamp.jpg")
}

private fun decodeForDisplay(file: File, maxWidthPx: Int): Pair<Bitmap, PixelSize> {
    val boundsOpts = BitmapFactory.Options().apply { inJustDecodeBounds = true }
    BitmapFactory.decodeFile(file.absolutePath, boundsOpts)
    val originalWidth = boundsOpts.outWidth
    val originalHeight = boundsOpts.outHeight
    var sampleSize = 1
    while (originalWidth / (sampleSize * 2) >= maxWidthPx) sampleSize *= 2
    val opts = BitmapFactory.Options().apply { inSampleSize = sampleSize }
    val bitmap = BitmapFactory.decodeFile(file.absolutePath, opts)
    return bitmap to PixelSize(originalWidth, originalHeight)
}

/**
 * pointsがnullなら自動（赤丸）検出モード、指定すればその4点座標で透視変換するモード。
 * サーバの判定結果に応じて3種類のコールバックのいずれかを呼ぶ。
 */
private suspend fun submitCalibration(
    file: File,
    points: List<List<Double>>?,
    onMatch: () -> Unit,
    onMismatch: (Int) -> Unit,
    onCalibrationFailed: () -> Unit,
    onError: (String) -> Unit
) {
    try {
        val requestBody = file.asRequestBody("image/jpeg".toMediaType())
        val part = MultipartBody.Part.createFormData("file", file.name, requestBody)
        val pointsBody = points?.let { Gson().toJson(it).toRequestBody("text/plain".toMediaType()) }
        val response = RetrofitClient.shogiApiService.calibrationPhoto(part, pointsBody)
        val body = response.body()
        when {
            !response.isSuccessful || body == null -> onError("HTTP ${response.code()}")
            body.status == "calibration_failed" -> onCalibrationFailed()
            body.matches_initial == true -> onMatch()
            else -> onMismatch(body.mismatch_count ?: -1)
        }
    } catch (e: Exception) {
        onError(e.message ?: "通信エラー")
    }
}

private suspend fun confirmCalibration(onSuccess: () -> Unit, onError: (String) -> Unit) {
    try {
        val response = RetrofitClient.shogiApiService.calibrationConfirm()
        if (response.isSuccessful) onSuccess() else onError("HTTP ${response.code()}")
    } catch (e: Exception) {
        onError(e.message ?: "通信エラー")
    }
}
