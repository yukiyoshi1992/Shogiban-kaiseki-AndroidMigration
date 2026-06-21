package com.example.shogiban_kaiseki_appli.calibration

import android.content.Context
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import androidx.camera.core.AspectRatio
import androidx.camera.core.CameraSelector
import androidx.camera.core.ImageCapture
import androidx.camera.core.ImageCaptureException
import androidx.camera.core.Preview
import androidx.camera.core.resolutionselector.AspectRatioStrategy
import androidx.camera.core.resolutionselector.ResolutionSelector
import androidx.camera.lifecycle.ProcessCameraProvider
import androidx.camera.view.PreviewView
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.Image
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
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.asImageBitmap
import androidx.compose.foundation.gestures.detectTapGestures
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.layout.onSizeChanged
import androidx.compose.ui.platform.LocalContext
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

private enum class CalibPhase { CAMERA, AUTO_SUBMITTING, TAPPING, SUBMITTING, ERROR }

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
    var tappingHint by remember { mutableStateOf("") }
    var errorMessage by remember { mutableStateOf("") }

    fun resetToCamera() {
        phase = CalibPhase.CAMERA
        capturedFile = null
        capturedBitmap = null
        tapPoints = emptyList()
    }

    fun proceedToConfirm() {
        coroutineScope.launch {
            confirmCalibration(
                onSuccess = { onCalibrated() },
                onError = { msg -> errorMessage = msg; phase = CalibPhase.ERROR }
            )
        }
    }

    // 自動検出（赤丸）モード：失敗・不一致いずれも人間の4隅タップにフォールバックする
    // （前PJ run_realtime.py と同じ「エラーが出たら即・人間が直す」方針）。
    fun handleAutoResult(file: File) {
        coroutineScope.launch {
            submitCalibration(file, points = null,
                onMatch = { proceedToConfirm() },
                onMismatch = { count ->
                    tappingHint = "自動認識が初期配置と${count}箇所異なりました。盤の四隅をタップしてください"
                    phase = CalibPhase.TAPPING
                },
                onCalibrationFailed = {
                    tappingHint = "赤丸を検出できませんでした。盤の四隅をタップしてください"
                    phase = CalibPhase.TAPPING
                },
                onError = { msg -> errorMessage = msg; phase = CalibPhase.ERROR }
            )
        }
    }

    // 手動4隅タップモード：人間が直接指定した結果は前PJの方針通り無条件に採用する
    // （初期配置との不一致があっても、人間が見て決めた4隅を信頼し、再度のやり直しは求めない）。
    fun handleManualResult(file: File, points: List<List<Double>>) {
        coroutineScope.launch {
            submitCalibration(file, points,
                onMatch = { proceedToConfirm() },
                onMismatch = { proceedToConfirm() },
                onCalibrationFailed = { errorMessage = "サーバ側でキャリブレーションに失敗しました"; phase = CalibPhase.ERROR },
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
                handleAutoResult(file)
            }
        )

        CalibPhase.AUTO_SUBMITTING -> Box(modifier = modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
            Text("赤丸を自動検出中...")
        }

        CalibPhase.TAPPING -> TappingStep(
            modifier = modifier,
            bitmap = capturedBitmap!!,
            points = tapPoints,
            hint = tappingHint,
            onPointsChanged = { tapPoints = it },
            onRetake = { resetToCamera() },
            onSubmit = {
                phase = CalibPhase.SUBMITTING
                val bmp = capturedBitmap!!
                val scaled = tapPoints.map { p ->
                    listOf(
                        (p.x / bmp.width * originalSize.width).toDouble(),
                        (p.y / bmp.height * originalSize.height).toDouble()
                    )
                }
                handleManualResult(capturedFile!!, scaled)
            }
        )

        CalibPhase.SUBMITTING -> Box(modifier = modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
            Text("サーバで判定中...")
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
    // imageCaptureは（PlayScreenのbyremember状態と違い）普通の関数パラメータなので、
    // capture()がそのまま参照すると「LaunchedEffect(Unit)が初回実行された時点のnull」を
    // 永遠に捉えたままになる（カメラ初期化完了後の再コンポーズで新しいimageCaptureが渡されても、
    // 音量キー側に登録済みのcapture()はそれを見ない）。これが「画面のボタンは効くが音量キーは
    // 何も起きない」バグの真因だった（2026-06-21、実機報告で確定）。rememberUpdatedStateで
    // 常に最新の値を読むようにする。
    val currentImageCapture = rememberUpdatedState(imageCapture)

    fun capture() {
        val capture = currentImageCapture.value ?: return
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
                    // CameraXの既定は4:3で、前PJのカメラ仕様（16:9、3840x2160）と画角が異なり、
                    // 盤の周囲の余白が増えて赤丸検出が不安定になっていた。前PJと同じ16:9を明示指定する
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

/** ズームイン表示で見せる範囲（実画像ピクセル座標系での切り出し矩形）。 */
private data class ZoomCrop(val left: Float, val top: Float, val width: Float, val height: Float)

private const val TAP_ZOOM_FACTOR = 4f

private fun computeZoomCrop(anchorBitmap: Offset, bitmap: Bitmap): ZoomCrop {
    val cropW = bitmap.width / TAP_ZOOM_FACTOR
    val cropH = bitmap.height / TAP_ZOOM_FACTOR
    val left = (anchorBitmap.x - cropW / 2f).coerceIn(0f, (bitmap.width - cropW).coerceAtLeast(0f))
    val top = (anchorBitmap.y - cropH / 2f).coerceIn(0f, (bitmap.height - cropH).coerceAtLeast(0f))
    return ZoomCrop(left, top, cropW, cropH)
}

/**
 * ボックス内で実際に画像が表示されている矩形（box座標系）。Image(contentScale=Fit、既定値)と
 * 同じロジック：箱と画像のアスペクト比が一致しない場合、余白（レターボックス/ピラーボックス）ができる。
 *
 * 2026-06-21、実機デバッグ表示の実測値から判明：このBoxは元々「箱の高さ＝幅/画像アスペクト比」
 * （`boxHeightDp = maxWidth / aspect`）として、箱と画像のアスペクト比を一致させる前提で設計されて
 * いたが、画面の縦スペースが足りない（ヒントテキスト等で消費される）場合はその高さ要求が
 * 親のColumnの`weight(1f)`で確保できる範囲に収まらずクランプされ、実際のbox高さが意図した値より
 * 低くなる（実測例：要求約1920pxに対し実測1510px）。この結果、箱の中の画像はContentScale.Fitにより
 * 左右に余白ができた状態で表示されるが、従来の座標変換（boxサイズ＝画像表示範囲とみなす単純比例）は
 * この余白を考慮しておらず、タップ位置が常に実際の画像上の位置とズレるバグの真因だった
 * （2回の独立したUI再設計でも再発し続けていたのは、この前提自体がどちらにも共通していたため）。
 * 箱の高さを画像アスペクトに無理に合わせようとするのではなく、実際のboxサイズに対して
 * このfitRectを都度計算し、タップ⇄画像座標の変換は必ずこれを経由するようにした。
 */
private data class FitRect(val left: Float, val top: Float, val width: Float, val height: Float)

private fun computeFitRect(boxSize: IntSize, contentWidth: Float, contentHeight: Float): FitRect {
    val boxW = boxSize.width.toFloat().coerceAtLeast(1f)
    val boxH = boxSize.height.toFloat().coerceAtLeast(1f)
    val boxAspect = boxW / boxH
    val contentAspect = contentWidth / contentHeight
    return if (boxAspect > contentAspect) {
        val h = boxH
        val w = h * contentAspect
        FitRect((boxW - w) / 2f, 0f, w, h)
    } else {
        val w = boxW
        val h = w / contentAspect
        FitRect(0f, (boxH - h) / 2f, w, h)
    }
}

/** 表示座標（box座標系）→ コンテンツ内座標（0..contentWidth/Height）。fitRectの外（余白）ならnull。 */
private fun FitRect.displayToContent(displayPos: Offset, contentWidth: Float, contentHeight: Float): Offset? {
    val localX = displayPos.x - left
    val localY = displayPos.y - top
    if (localX < 0f || localY < 0f || localX > width || localY > height) return null
    return Offset(localX / width * contentWidth, localY / height * contentHeight)
}

private fun FitRect.contentToDisplay(contentPos: Offset, contentWidth: Float, contentHeight: Float): Offset {
    return Offset(left + contentPos.x / contentWidth * width, top + contentPos.y / contentHeight * height)
}

/**
 * 盤の四隅タップ用ステップ。「①概観でおおまかにタップ→②その周辺が拡大表示される→
 * ③拡大表示の中で正確に角をタップして確定→④概観に戻る」を4回繰り返す方式
 * （2026-06-21、「ドラッグ＋ルーペ」方式がどう使えばいいか分からないというユーザー指摘を受け、
 * UIを全面的に作り直した）。
 */
@Composable
private fun TappingStep(
    modifier: Modifier,
    bitmap: Bitmap,
    points: List<Offset>, // ビットマップ画素座標系（decodeForDisplay()が返したbitmapそのものの座標）
    hint: String,
    onPointsChanged: (List<Offset>) -> Unit,
    onRetake: () -> Unit,
    onSubmit: () -> Unit
) {
    val bitmapW = bitmap.width.toFloat()
    val bitmapH = bitmap.height.toFloat()
    var measuredSize by remember { mutableStateOf(IntSize(1, 1)) }
    var zoomAnchorBitmap by remember { mutableStateOf<Offset?>(null) }
    // 2026-06-21、実機デバッグ表示の実測値から「箱の表示領域＝box全体」という前提が間違っていた
    // ことが判明（上のFitRectのコメント参照）。原因確定後もこの表示はそのまま残し、次の実機テストで
    // 数値が正しくなったことを確認できるようにする。
    var debugInfo by remember { mutableStateOf("") }

    // pointerInputのkey変更による再起動タイミングに依存すると、タップ直後の状態更新が
    // ジェスチャ検出側にまだ反映されておらず「ズームインしたのに2回目のタップが
    // ズームアウト時の座標として解釈される」不具合が起きていた。rememberUpdatedStateで
    // 常に最新の値を読むようにし、pointerInput自体はUnitキーで一度だけ起動して
    // 再起動タイミングの問題を構造的に回避する（2026-06-21）。
    val currentCrop = rememberUpdatedState(zoomAnchorBitmap?.let { computeZoomCrop(it, bitmap) })
    val currentPoints = rememberUpdatedState(points)
    val currentMeasuredSize = rememberUpdatedState(measuredSize)

    Column(modifier = modifier.fillMaxSize()) {
        if (hint.isNotEmpty()) {
            Text(text = hint, modifier = Modifier.padding(8.dp))
        }
        Text(
            text = if (zoomAnchorBitmap == null) {
                "盤の四隅（マス目の角、駒台は不要）のあたりをタップしてください（${points.size}/4）"
            } else {
                "拡大表示されました。角を正確にタップして確定してください"
            },
            modifier = Modifier.padding(8.dp)
        )
        if (debugInfo.isNotEmpty()) {
            Text(
                text = debugInfo,
                modifier = Modifier.padding(horizontal = 8.dp),
                style = MaterialTheme.typography.bodySmall
            )
        }
        Box(
            modifier = Modifier
                .fillMaxWidth()
                .weight(1f)
                .onSizeChanged { measuredSize = it }
                .pointerInput(Unit) {
                    detectTapGestures { tapPos ->
                        val boxSize = currentMeasuredSize.value
                        val fitRect = computeFitRect(boxSize, bitmapW, bitmapH)
                        val cropNow = currentCrop.value
                        if (cropNow == null) {
                            if (currentPoints.value.size < 4) {
                                val anchor = fitRect.displayToContent(tapPos, bitmapW, bitmapH)
                                debugInfo = "①ズーム開始 disp=$tapPos boxSize=$boxSize fitRect=$fitRect → anchor=$anchor"
                                if (anchor != null) zoomAnchorBitmap = anchor
                            }
                        } else {
                            // ズーム中も表示領域は同じfitRect（クロップ矩形は常に元画像と同じアスペクト比のため）。
                            // fitRect内でのタップ位置の比率を、クロップ矩形（ビットマップ座標）に適用する。
                            val localX = ((tapPos.x - fitRect.left) / fitRect.width).coerceIn(0f, 1f)
                            val localY = ((tapPos.y - fitRect.top) / fitRect.height).coerceIn(0f, 1f)
                            val point = Offset(
                                cropNow.left + localX * cropNow.width,
                                cropNow.top + localY * cropNow.height
                            )
                            debugInfo = "②確定タップ disp=$tapPos boxSize=$boxSize fitRect=$fitRect crop=$cropNow → point(bitmap座標)=$point"
                            onPointsChanged(currentPoints.value + point)
                            zoomAnchorBitmap = null
                        }
                    }
                }
        ) {
            val fitRect = computeFitRect(measuredSize, bitmapW, bitmapH)
            val crop = zoomAnchorBitmap?.let { computeZoomCrop(it, bitmap) }
            if (crop == null) {
                Image(
                    bitmap = bitmap.asImageBitmap(),
                    contentDescription = null,
                    modifier = Modifier.fillMaxSize()
                )
                Canvas(modifier = Modifier.fillMaxSize()) {
                    points.forEach { p ->
                        drawCircle(color = Color.Red, radius = 12f, center = fitRect.contentToDisplay(p, bitmapW, bitmapH))
                    }
                }
            } else {
                Canvas(modifier = Modifier.fillMaxSize()) {
                    val dstOffset = IntOffset(fitRect.left.roundToInt(), fitRect.top.roundToInt())
                    val dstSize = IntSize(
                        fitRect.width.roundToInt().coerceAtLeast(1),
                        fitRect.height.roundToInt().coerceAtLeast(1)
                    )
                    drawImage(
                        image = bitmap.asImageBitmap(),
                        srcOffset = IntOffset(crop.left.roundToInt(), crop.top.roundToInt()),
                        srcSize = IntSize(
                            crop.width.roundToInt().coerceAtLeast(1),
                            crop.height.roundToInt().coerceAtLeast(1)
                        ),
                        dstOffset = dstOffset,
                        dstSize = dstSize
                    )
                    // 拡大範囲内に確定済みの点があれば参考として表示する。
                    points.forEach { p ->
                        if (p.x in crop.left..(crop.left + crop.width) &&
                            p.y in crop.top..(crop.top + crop.height)
                        ) {
                            val fracX = (p.x - crop.left) / crop.width
                            val fracY = (p.y - crop.top) / crop.height
                            val dispPos = Offset(
                                dstOffset.x + fracX * dstSize.width,
                                dstOffset.y + fracY * dstSize.height
                            )
                            drawCircle(color = Color.Red, radius = 8f, center = dispPos)
                        }
                    }
                    val centerX = dstOffset.x + dstSize.width / 2f
                    val centerY = dstOffset.y + dstSize.height / 2f
                    drawLine(Color.Yellow, Offset(centerX - 20f, centerY), Offset(centerX + 20f, centerY), strokeWidth = 3f)
                    drawLine(Color.Yellow, Offset(centerX, centerY - 20f), Offset(centerX, centerY + 20f), strokeWidth = 3f)
                }
            }
        }
        Row(
            modifier = Modifier.fillMaxWidth().padding(16.dp),
            horizontalArrangement = Arrangement.SpaceEvenly
        ) {
            if (zoomAnchorBitmap != null) {
                Button(onClick = { zoomAnchorBitmap = null }) { Text("ズーム解除") }
            } else {
                Button(onClick = onRetake) { Text("撮り直す") }
                Button(onClick = { onPointsChanged(emptyList()) }, enabled = points.isNotEmpty()) { Text("タップをやり直す") }
                Button(enabled = points.size == 4, onClick = onSubmit) { Text("送信") }
            }
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
        // 2026-06-21、タイムアウト原因調査用：アップロード完了までの時間と応答待ちの時間を
        // 画面のエラー表示に含める（Logcatを見られない状況でも切り分けられるようにするため）。
        onError("${e.message ?: "通信エラー"} [${com.example.shogiban_kaiseki_appli.network.NetworkTiming.lastSummary}]")
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
