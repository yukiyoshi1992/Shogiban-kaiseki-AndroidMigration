package com.example.shogiban_kaiseki_appli.network

import okhttp3.Call
import okhttp3.EventListener
import java.io.IOException

/**
 * 直近のRetrofit通信の各フェーズ（アップロード完了までの時間／応答待ちの時間）を保持する。
 *
 * 2026-06-21、「サーバはuvicornログ上200 OKで2秒程度で完了しているのに、クライアント側だけ
 * タイムアウトする」という事象の調査用。サーバ側の処理時間ログ（main.pyの`[timing]`）は
 * FastAPIがmultipartボディを全部受信し終えてからハンドラを呼ぶ関係で、肝心の「アップロードに
 * かかった時間」を測れていなかった（測定開始が遅すぎた）ため、別の地点（クライアント側）で
 * 直接計測する必要がある。エラー発生時にこの内容をエラーメッセージに含めて画面に出すことで、
 * Logcatを見られない状況でもアップロード時間か応答待ち時間かをその場で判別できるようにする。
 */
object NetworkTiming {
    @Volatile
    var lastSummary: String = ""
        private set

    internal fun record(summary: String) {
        lastSummary = summary
    }
}

class TimingEventListener : EventListener() {
    private var callStartMs = 0L
    private var requestBodyEndMs = 0L
    private var responseHeadersStartMs = 0L

    private fun now() = System.currentTimeMillis()

    override fun callStart(call: Call) {
        callStartMs = now()
        requestBodyEndMs = 0L
        responseHeadersStartMs = 0L
    }

    override fun requestBodyEnd(call: Call, byteCount: Long) {
        requestBodyEndMs = now()
    }

    override fun responseHeadersStart(call: Call) {
        responseHeadersStartMs = now()
    }

    override fun callEnd(call: Call) {
        finish("成功")
    }

    override fun callFailed(call: Call, ioe: IOException) {
        finish("失敗(${ioe.javaClass.simpleName}: ${ioe.message})")
    }

    private fun finish(result: String) {
        val endMs = now()
        val uploadMs = if (requestBodyEndMs > 0) requestBodyEndMs - callStartMs else -1
        val waitMs = if (responseHeadersStartMs > 0 && requestBodyEndMs > 0) {
            responseHeadersStartMs - requestBodyEndMs
        } else -1
        NetworkTiming.record(
            "結果=$result アップロード=${uploadMs}ms 応答待ち=${waitMs}ms 合計=${endMs - callStartMs}ms"
        )
    }
}
