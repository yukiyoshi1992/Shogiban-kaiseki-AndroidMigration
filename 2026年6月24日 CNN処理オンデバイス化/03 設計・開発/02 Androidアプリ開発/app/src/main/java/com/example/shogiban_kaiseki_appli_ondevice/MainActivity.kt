package com.example.shogiban_kaiseki_appli_ondevice

import android.content.Context
import android.graphics.BitmapFactory
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.unit.dp
import com.example.shogiban_kaiseki_appli_ondevice.network.RetrofitClient
import com.example.shogiban_kaiseki_appli_ondevice.recognition.OnDeviceRecognition
import com.example.shogiban_kaiseki_appli_ondevice.recognition.TestPhoto
import com.example.shogiban_kaiseki_appli_ondevice.recognition.TestPhotos
import com.example.shogiban_kaiseki_appli_ondevice.ui.theme.ShogibankaisekiappliTheme
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.MultipartBody
import okhttp3.RequestBody.Companion.asRequestBody
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONArray
import org.opencv.android.OpenCVLoader
import org.pytorch.LiteModuleLoader
import org.pytorch.Module
import java.io.File
import java.io.FileOutputStream

/**
 * CNN処理オンデバイス化の検証PoC専用アプリ。本番アプリ（CameraX・対局フロー）は持たず、
 * バンドル済みのテスト写真2枚（assets、PCで自動検出済みの4隅座標つき）に対して、
 * ①オンデバイス（OpenCV+PyTorch Mobile Lite）と②サーバー（既存recognition.pyと
 * 同じコードをベンチマーク専用ポートで実行）の両方で同じ盤面認識を行い、処理時間を
 * 比較するためだけの画面。
 */
class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge()
        val openCvOk = OpenCVLoader.initLocal()
        setContent {
            ShogibankaisekiappliTheme {
                Scaffold(modifier = Modifier.fillMaxSize()) { innerPadding ->
                    BenchmarkScreen(modifier = Modifier.padding(innerPadding), openCvOk = openCvOk)
                }
            }
        }
    }
}

/** assets内の.ptlを一度だけfilesDirにコピーする（PyTorch Mobileは実ファイルパスが必要なため） */
private fun assetFilePath(context: Context, assetName: String): String {
    val file = File(context.filesDir, assetName)
    if (!file.exists() || file.length() == 0L) {
        context.assets.open(assetName).use { input ->
            FileOutputStream(file).use { output -> input.copyTo(output) }
        }
    }
    return file.absolutePath
}

private fun pointsToJson(points: List<org.opencv.core.Point>): String {
    val arr = JSONArray()
    for (p in points) {
        val pair = JSONArray()
        pair.put(p.x)
        pair.put(p.y)
        arr.put(pair)
    }
    return arr.toString()
}

private fun gridsMatch(a: List<List<String>>, b: List<List<String>>): Boolean {
    for (row in a.indices) {
        for (col in a[row].indices) {
            if (a[row][col] != b[row][col]) return false
        }
    }
    return true
}

@Composable
private fun BenchmarkScreen(modifier: Modifier = Modifier, openCvOk: Boolean) {
    val context = LocalContext.current
    val scope = rememberCoroutineScope()

    var selectedPhoto by remember { mutableStateOf<TestPhoto>(TestPhotos.PHOTO_1) }
    var module by remember { mutableStateOf<Module?>(null) }
    var resultText by remember { mutableStateOf("「オンデバイスで実行」または「サーバーで実行」を押してください") }
    var busy by remember { mutableStateOf(false) }

    Column(
        modifier = modifier.fillMaxSize().padding(16.dp).verticalScroll(rememberScrollState()),
        verticalArrangement = Arrangement.spacedBy(12.dp)
    ) {
        Text("CNN処理オンデバイス化 検証PoC", style = MaterialTheme.typography.titleLarge)
        Text(if (openCvOk) "OpenCV読み込み: OK" else "OpenCV読み込み: 失敗")

        Text("テスト写真:")
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            TestPhotos.ALL.forEachIndexed { i, photo ->
                Button(onClick = { selectedPhoto = photo }) {
                    Text("写真${i + 1}${if (photo == selectedPhoto) "（選択中）" else ""}")
                }
            }
        }

        Button(
            enabled = !busy,
            onClick = {
                busy = true
                resultText = "オンデバイス推論を実行中..."
                scope.launch {
                    try {
                        val text = withContext(Dispatchers.Default) {
                            val m = module ?: LiteModuleLoader.load(
                                assetFilePath(context, "board_classifier.ptl")
                            ).also { module = it }
                            val bitmap = context.assets.open(selectedPhoto.assetName).use {
                                BitmapFactory.decodeStream(it)
                            }
                            val result = OnDeviceRecognition.run(m, bitmap, selectedPhoto.corners)
                            val matches = gridsMatch(result.labels, TestPhotos.EXPECTED_INITIAL_POSITION)
                            "【オンデバイス】\n" +
                                "透視変換: ${result.warpMs}ms\n" +
                                "前処理（81マス切り出し・リサイズ・正規化）: ${result.preprocessMs}ms\n" +
                                "CNN推論: ${result.inferenceMs}ms\n" +
                                "合計: ${result.totalMs}ms\n" +
                                "初期配置と一致: ${if (matches) "○" else "× (要確認)"}"
                        }
                        resultText = text
                    } catch (e: Exception) {
                        resultText = "オンデバイス推論でエラー: ${e.javaClass.simpleName}: ${e.message}"
                    } finally {
                        busy = false
                    }
                }
            }
        ) { Text("オンデバイスで実行") }

        Button(
            enabled = !busy,
            onClick = {
                busy = true
                resultText = "サーバーへ送信中..."
                scope.launch {
                    try {
                        val text = withContext(Dispatchers.IO) {
                            val tmpFile = File(context.cacheDir, selectedPhoto.assetName)
                            context.assets.open(selectedPhoto.assetName).use { input ->
                                FileOutputStream(tmpFile).use { output -> input.copyTo(output) }
                            }
                            val filePart = MultipartBody.Part.createFormData(
                                "file", tmpFile.name,
                                tmpFile.asRequestBody("image/jpeg".toMediaType())
                            )
                            val pointsPart = pointsToJson(selectedPhoto.corners)
                                .toRequestBody("text/plain".toMediaType())
                            val t0 = System.currentTimeMillis()
                            val response = RetrofitClient.shogiApiService.calibrationPhoto(filePart, pointsPart)
                            val elapsedMs = System.currentTimeMillis() - t0
                            if (!response.isSuccessful) {
                                "【サーバー】HTTPエラー: ${response.code()}\n" +
                                    "(注意：8001番ポートでベンチマーク専用サーバーを起動済みか確認してください。" +
                                    "本番8000番は使わない設計です)"
                            } else {
                                val body = response.body()
                                "【サーバー】\n" +
                                    "往復時間（クライアント計測）: ${elapsedMs}ms\n" +
                                    "status: ${body?.status}\n" +
                                    "初期配置と一致: ${if (body?.matches_target == true) "○" else "× (要確認)"}"
                            }
                        }
                        resultText = text
                    } catch (e: Exception) {
                        resultText = "サーバー呼び出しでエラー: ${e.javaClass.simpleName}: ${e.message}\n" +
                            "(8001番ポートでベンチマーク専用サーバーが起動しているか確認してください)"
                    } finally {
                        busy = false
                    }
                }
            }
        ) { Text("サーバーで実行（比較用、要：別ポートでサーバー起動）") }

        Text(resultText)
    }
}
