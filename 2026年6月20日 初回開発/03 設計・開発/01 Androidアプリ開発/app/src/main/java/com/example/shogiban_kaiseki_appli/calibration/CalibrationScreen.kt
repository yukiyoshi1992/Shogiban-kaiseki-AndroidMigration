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
import androidx.compose.foundation.gestures.detectTapGestures
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
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.layout.onSizeChanged
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalLifecycleOwner
import androidx.compose.ui.unit.dp
import androidx.compose.ui.viewinterop.AndroidView
import androidx.core.content.ContextCompat
import com.example.shogiban_kaiseki_appli.board.labelToDisplay
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

private enum class CalibPhase { CAMERA, TAPPING, SUBMITTING, CONFIRMING, ERROR }

/** decodeForDisplay()が返す元画像（撮影フルサイズ）のピクセル寸法。タップ座標をこの基準に変換して送信する。 */
private data class PixelSize(val width: Int, val height: Int)

/**
 * キャリブレーション画面：①撮影 → ②盤の四隅4点タップ → ③サーバへ送信し9x9認識結果を確認
 * → ④OKなら対局開始、ズレていれば①からやり直し（個別マス補正はしない、アーキテクチャ検討.md確定事項）。
 */
@Composable
fun CalibrationScreen(modifier: Modifier = Modifier, onCalibrated: () -> Unit) {
    val coroutineScope = rememberCoroutineScope()

    var phase by remember { mutableStateOf(CalibPhase.CAMERA) }
    var imageCapture by remember { mutableStateOf<ImageCapture?>(null) }
    var capturedFile by remember { mutableStateOf<File?>(null) }
    var capturedBitmap by remember { mutableStateOf<Bitmap?>(null) }
    var originalSize by remember { mutableStateOf(PixelSize(0, 0)) }
    var tapPoints by remember { mutableStateOf(listOf<Offset>()) }
    var recognized by remember { mutableStateOf<List<List<String>>?>(null) }
    var errorMessage by remember { mutableStateOf("") }

    fun resetToCamera() {
        phase = CalibPhase.CAMERA
        capturedFile = null
        capturedBitmap = null
        tapPoints = emptyList()
        recognized = null
    }

    when (phase) {
        CalibPhase.CAMERA -> CameraCaptureStep(
            modifier = modifier,
            imageCapture = imageCapture,
            onImageCaptureReady = { imageCapture = it },
            onCaptured = { file ->
                val (bitmap, size) = decodeForDisplay(file, maxWidthPx = 1600)
                capturedFile = file
                capturedBitmap = bitmap
                originalSize = size
                tapPoints = emptyList()
                phase = CalibPhase.TAPPING
            }
        )

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
                coroutineScope.launch {
                    submitCalibration(capturedFile!!, scaled,
                        onSuccess = { grid -> recognized = grid; phase = CalibPhase.CONFIRMING },
                        onError = { msg -> errorMessage = msg; phase = CalibPhase.ERROR }
                    )
                }
            }
        )

        CalibPhase.SUBMITTING -> Box(modifier = modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
            Text("サーバに送信中...")
        }

        CalibPhase.CONFIRMING -> ConfirmStep(
            modifier = modifier,
            recognized = recognized ?: emptyList(),
            onRedo = { resetToCamera() },
            onConfirm = {
                coroutineScope.launch {
                    confirmCalibration(
                        onSuccess = { onCalibrated() },
                        onError = { msg -> errorMessage = msg; phase = CalibPhase.ERROR }
                    )
                }
            }
        )

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
    onCaptured: (File) -> Unit
) {
    val context = LocalContext.current
    val lifecycleOwner = LocalLifecycleOwner.current
    val playShutterSound = rememberShutterSound()

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
            Text("盤全体が入るように撮影してください")
            Button(
                modifier = Modifier.size(72.dp),
                shape = CircleShape,
                onClick = {
                    val capture = imageCapture ?: return@Button
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
            ) {}
        }
    }
}

@Composable
private fun TappingStep(
    modifier: Modifier,
    bitmap: Bitmap,
    points: List<Offset>,
    onPointsChanged: (List<Offset>) -> Unit,
    onRetake: () -> Unit,
    onSubmit: (boxSizePx: PixelSize) -> Unit
) {
    val aspect = bitmap.width.toFloat() / bitmap.height.toFloat()
    var measuredSize by remember { mutableStateOf(PixelSize(1, 1)) }

    Column(modifier = modifier.fillMaxSize()) {
        Text(
            text = "盤の四隅（マス目の角、駒台は不要）を順不同で4回タップしてください（${points.size}/4）",
            modifier = Modifier.padding(8.dp)
        )
        BoxWithConstraints(modifier = Modifier.fillMaxWidth().weight(1f)) {
            val boxHeightDp = maxWidth / aspect
            Box(
                modifier = Modifier
                    .fillMaxWidth()
                    .height(boxHeightDp)
                    .onSizeChanged { measuredSize = PixelSize(it.width, it.height) }
                    .pointerInput(points.size) {
                        detectTapGestures { offset ->
                            if (points.size < 4) onPointsChanged(points + offset)
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

private suspend fun submitCalibration(
    file: File,
    points: List<List<Double>>,
    onSuccess: (List<List<String>>) -> Unit,
    onError: (String) -> Unit
) {
    try {
        val requestBody = file.asRequestBody("image/jpeg".toMediaType())
        val part = MultipartBody.Part.createFormData("file", file.name, requestBody)
        val pointsJson = Gson().toJson(points)
        val pointsBody = pointsJson.toRequestBody("text/plain".toMediaType())
        val response = RetrofitClient.shogiApiService.calibrationPhoto(part, pointsBody)
        val body = response.body()
        if (response.isSuccessful && body?.recognized != null) {
            onSuccess(body.recognized)
        } else {
            onError("HTTP ${response.code()}")
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

@Composable
private fun ConfirmStep(
    modifier: Modifier,
    recognized: List<List<String>>,
    onRedo: () -> Unit,
    onConfirm: () -> Unit
) {
    Column(modifier = modifier.fillMaxSize().padding(16.dp), horizontalAlignment = Alignment.CenterHorizontally) {
        Text("認識結果を確認してください（初期配置と一致していればOK）")
        Column(modifier = Modifier.padding(8.dp)) {
            recognized.forEach { row ->
                Row {
                    row.forEach { label ->
                        Text(text = labelToDisplay(label), modifier = Modifier.size(36.dp).padding(1.dp))
                    }
                }
            }
        }
        Row(horizontalArrangement = Arrangement.SpaceEvenly, modifier = Modifier.fillMaxWidth()) {
            Button(onClick = onRedo) { Text("やり直す（4隅タップから）") }
            Button(onClick = onConfirm) { Text("OK・対局開始") }
        }
    }
}
