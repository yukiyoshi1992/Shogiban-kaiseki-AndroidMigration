package com.example.shogiban_kaiseki_appli_ondevice.recognition

import android.graphics.Bitmap
import org.opencv.android.Utils
import org.opencv.core.Mat
import org.opencv.core.MatOfPoint2f
import org.opencv.core.Point
import org.opencv.core.Rect
import org.opencv.core.Size
import org.opencv.imgproc.Imgproc
import org.pytorch.IValue
import org.pytorch.Module
import org.pytorch.Tensor

/**
 * 00 runtime/server/recognition.py の warp_board・predict_board と同じ処理
 * （透視変換→81マスに分割→各マスを224x224にリサイズ・ImageNet正規化→CNN推論）を
 * Android上でOpenCV+PyTorch Mobile Liteを使って再現したもの。
 * 既存の本番コードはimportせず、構造だけ複製している（PJの前提「既存コードは変更しない」
 * に従うため、独立した複製として実装）。
 */
object OnDeviceRecognition {
    const val GRID_SIZE = 9
    const val CELL_PX = 100
    const val WARP_SIDE = CELL_PX * GRID_SIZE // 900
    const val MODEL_INPUT = 224

    // recognition.py ALL_LABELSと同一（クラス数29、インデックス順も同じ）
    val ALL_LABELS = listOf(
        "empty",
        "sente_fu", "sente_kyo", "sente_kei", "sente_gin", "sente_kin",
        "sente_kaku", "sente_hi", "sente_ou",
        "sente_tokin", "sente_nari_kyo", "sente_nari_kei", "sente_nari_gin",
        "sente_uma", "sente_ryu",
        "gote_fu", "gote_kyo", "gote_kei", "gote_gin", "gote_kin",
        "gote_kaku", "gote_hi", "gote_ou",
        "gote_tokin", "gote_nari_kyo", "gote_nari_kei", "gote_nari_gin",
        "gote_uma", "gote_ryu",
    )

    // torchvision transforms.Normalize([0.485,0.456,0.406],[0.229,0.224,0.225])と同一
    private val MEAN = floatArrayOf(0.485f, 0.456f, 0.406f)
    private val STD = floatArrayOf(0.229f, 0.224f, 0.225f)

    data class Result(
        val labels: List<List<String>>,
        val warpMs: Long,
        val preprocessMs: Long,
        val inferenceMs: Long,
        val totalMs: Long
    )

    /** corners: 画像のピクセル座標、TL・TR・BR・BLの順（recognition.order_pointsと同じ並び） */
    fun run(module: Module, bitmap: Bitmap, corners: List<Point>): Result {
        val t0 = System.nanoTime()

        val rgbaMat = Mat()
        Utils.bitmapToMat(bitmap, rgbaMat)
        val bgrMat = Mat()
        // cv2.imread相当（BGR）に揃える。最終的にはどのみち各マスでBGR2RGBするため、
        // ここでBGRに変換しておくのはサーバ側ロジックとの対応を分かりやすくするため。
        Imgproc.cvtColor(rgbaMat, bgrMat, Imgproc.COLOR_RGBA2BGR)
        rgbaMat.release()

        val srcPoints = MatOfPoint2f(corners[0], corners[1], corners[2], corners[3])
        val side = WARP_SIDE.toDouble()
        val dstPoints = MatOfPoint2f(
            Point(0.0, 0.0), Point(side, 0.0), Point(side, side), Point(0.0, side)
        )
        val transform = Imgproc.getPerspectiveTransform(srcPoints, dstPoints)
        val warped = Mat()
        Imgproc.warpPerspective(bgrMat, warped, transform, Size(side, side))
        bgrMat.release()

        val t1 = System.nanoTime()

        val numCells = GRID_SIZE * GRID_SIZE
        val plane = MODEL_INPUT * MODEL_INPUT
        val inputData = FloatArray(numCells * 3 * plane)
        val pixelBytes = ByteArray(plane * 3)

        for (row in 0 until GRID_SIZE) {
            for (col in 0 until GRID_SIZE) {
                val idx = row * GRID_SIZE + col
                val cell = Mat(warped, Rect(col * CELL_PX, row * CELL_PX, CELL_PX, CELL_PX))
                val cellRgb = Mat()
                Imgproc.cvtColor(cell, cellRgb, Imgproc.COLOR_BGR2RGB)
                val resized = Mat()
                Imgproc.resize(cellRgb, resized, Size(MODEL_INPUT.toDouble(), MODEL_INPUT.toDouble()))
                resized.get(0, 0, pixelBytes)

                val base = idx * 3 * plane
                for (p in 0 until plane) {
                    val r = (pixelBytes[p * 3].toInt() and 0xFF) / 255f
                    val g = (pixelBytes[p * 3 + 1].toInt() and 0xFF) / 255f
                    val b = (pixelBytes[p * 3 + 2].toInt() and 0xFF) / 255f
                    inputData[base + p] = (r - MEAN[0]) / STD[0]
                    inputData[base + plane + p] = (g - MEAN[1]) / STD[1]
                    inputData[base + 2 * plane + p] = (b - MEAN[2]) / STD[2]
                }
                cell.release()
                cellRgb.release()
                resized.release()
            }
        }
        warped.release()

        val t2 = System.nanoTime()

        val inputTensor = Tensor.fromBlob(
            inputData, longArrayOf(numCells.toLong(), 3, MODEL_INPUT.toLong(), MODEL_INPUT.toLong())
        )
        val outputTensor = module.forward(IValue.from(inputTensor)).toTensor()
        val scores = outputTensor.dataAsFloatArray // [numCells * ALL_LABELS.size]

        val t3 = System.nanoTime()

        val numClasses = ALL_LABELS.size
        val labels = MutableList(GRID_SIZE) { MutableList(GRID_SIZE) { "" } }
        for (idx in 0 until numCells) {
            var best = 0
            var bestScore = scores[idx * numClasses]
            for (c in 1 until numClasses) {
                val s = scores[idx * numClasses + c]
                if (s > bestScore) {
                    bestScore = s
                    best = c
                }
            }
            labels[idx / GRID_SIZE][idx % GRID_SIZE] = ALL_LABELS[best]
        }

        return Result(
            labels = labels,
            warpMs = (t1 - t0) / 1_000_000,
            preprocessMs = (t2 - t1) / 1_000_000,
            inferenceMs = (t3 - t2) / 1_000_000,
            totalMs = (t3 - t0) / 1_000_000
        )
    }
}
