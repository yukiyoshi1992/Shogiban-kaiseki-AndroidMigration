package com.example.shogiban_kaiseki_appli.camera

import android.content.Context
import androidx.camera.core.CameraSelector
import androidx.camera.core.ImageCapture
import androidx.camera.core.ImageCaptureException
import androidx.camera.core.Preview
import androidx.camera.lifecycle.ProcessCameraProvider
import androidx.camera.view.PreviewView
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
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
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalLifecycleOwner
import androidx.compose.ui.unit.dp
import androidx.compose.ui.viewinterop.AndroidView
import androidx.core.content.ContextCompat
import com.example.shogiban_kaiseki_appli.network.RetrofitClient
import kotlinx.coroutines.launch
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.MultipartBody
import okhttp3.RequestBody.Companion.asRequestBody
import java.io.File
import java.text.SimpleDateFormat
import java.util.Locale

private enum class UploadStatus {
    IDLE, CAPTURING, UPLOADING, SUCCESS, ERROR
}

@Composable
fun CameraScreen(modifier: Modifier = Modifier) {
    val context = LocalContext.current
    val lifecycleOwner = LocalLifecycleOwner.current
    val coroutineScope = rememberCoroutineScope()

    var imageCapture by remember { mutableStateOf<ImageCapture?>(null) }
    var status by remember { mutableStateOf(UploadStatus.IDLE) }
    var statusMessage by remember { mutableStateOf("") }

    Box(modifier = modifier.fillMaxSize()) {
        AndroidView(
            modifier = Modifier.fillMaxSize(),
            factory = { ctx ->
                val previewView = PreviewView(ctx)
                val cameraProviderFuture = ProcessCameraProvider.getInstance(ctx)
                cameraProviderFuture.addListener({
                    val cameraProvider = cameraProviderFuture.get()
                    val preview = Preview.Builder().build().also {
                        it.surfaceProvider = previewView.surfaceProvider
                    }
                    val capture = ImageCapture.Builder().build()
                    imageCapture = capture

                    cameraProvider.unbindAll()
                    cameraProvider.bindToLifecycle(
                        lifecycleOwner,
                        CameraSelector.DEFAULT_BACK_CAMERA,
                        preview,
                        capture
                    )
                }, ContextCompat.getMainExecutor(ctx))
                previewView
            }
        )

        Column(
            modifier = Modifier
                .fillMaxWidth()
                .align(Alignment.BottomCenter)
                .background(MaterialTheme.colorScheme.surface.copy(alpha = 0.85f))
                .padding(24.dp),
            horizontalAlignment = Alignment.CenterHorizontally
        ) {
            if (statusMessage.isNotEmpty()) {
                Text(text = statusMessage)
            }
            Button(
                modifier = Modifier.size(72.dp),
                shape = CircleShape,
                enabled = status != UploadStatus.CAPTURING && status != UploadStatus.UPLOADING,
                onClick = {
                    val capture = imageCapture ?: return@Button
                    status = UploadStatus.CAPTURING
                    statusMessage = "撮影中..."
                    val photoFile = createTempPhotoFile(context)
                    val outputOptions = ImageCapture.OutputFileOptions.Builder(photoFile).build()
                    capture.takePicture(
                        outputOptions,
                        ContextCompat.getMainExecutor(context),
                        object : ImageCapture.OnImageSavedCallback {
                            override fun onImageSaved(output: ImageCapture.OutputFileResults) {
                                status = UploadStatus.UPLOADING
                                statusMessage = "アップロード中..."
                                coroutineScope.launch {
                                    uploadPhoto(photoFile) { success, message ->
                                        status = if (success) UploadStatus.SUCCESS else UploadStatus.ERROR
                                        statusMessage = message
                                    }
                                }
                            }

                            override fun onError(exception: ImageCaptureException) {
                                status = UploadStatus.ERROR
                                statusMessage = "撮影失敗: ${exception.message}"
                            }
                        }
                    )
                }
            ) {}
        }
    }
}

private fun createTempPhotoFile(context: Context): File {
    val timestamp = SimpleDateFormat("yyyyMMdd_HHmmss", Locale.US).format(java.util.Date())
    return File(context.cacheDir, "capture_$timestamp.jpg")
}

private suspend fun uploadPhoto(file: File, onResult: (Boolean, String) -> Unit) {
    try {
        val requestBody = file.asRequestBody("image/jpeg".toMediaType())
        val part = MultipartBody.Part.createFormData("file", file.name, requestBody)
        val response = RetrofitClient.photoApiService.uploadPhoto(part)
        if (response.isSuccessful) {
            onResult(true, "送信成功: ${response.body()?.saved_as ?: ""}")
        } else {
            onResult(false, "送信失敗: HTTP ${response.code()}")
        }
    } catch (e: Exception) {
        onResult(false, "通信エラー: ${e.message}")
    }
}
