package com.example.shogiban_kaiseki_appli.network

import okhttp3.ConnectionPool
import okhttp3.OkHttpClient
import okhttp3.logging.HttpLoggingInterceptor
import retrofit2.Retrofit
import retrofit2.converter.gson.GsonConverterFactory
import java.util.concurrent.TimeUnit

object RetrofitClient {
    private val retrofit: Retrofit by lazy {
        val logging = HttpLoggingInterceptor().apply {
            level = HttpLoggingInterceptor.Level.BASIC
        }
        val client = OkHttpClient.Builder()
            .addInterceptor(logging)
            // 2026-06-21、タイムアウト原因調査用。アップロード完了までの時間と応答待ちの時間を
            // 分けて計測し、NetworkTiming.lastSummaryに記録する（詳細はNetworkTiming.kt参照）。
            .eventListener(TimingEventListener())
            .connectTimeout(10, TimeUnit.SECONDS)
            // 2026-06-22、診断用5分延長は撤回。真因は/calibration/photoではなく、その直後に
            // 自動送信していた2回目のリクエスト（旧/calibration/confirm）が断続的にタイムアウト
            // していたことだった（Logcatで確定）。その2回目自体を1リクエストへ統合して廃止した
            // ため、実測で常に2〜3秒で終わる/calibration/photo単体に60秒は十分な余裕がある。
            .readTimeout(60, TimeUnit.SECONDS)
            .writeTimeout(60, TimeUnit.SECONDS)
            // 2026-06-22、UAT課題②で/calibration/confirm_gridという「直前の通信の後に
            // 人間の判断を挟んでから送る2回目のリクエスト」を再び導入したため、念のため
            // keep-alive接続の再利用を無効化（maxIdleConnections=0）。直前のタイムアウト調査で
            // 「2回目のリクエストがサーバのハンドラに到達する前に失われていた」事象があり、
            // 正確な機序は未特定だがOkHttpの接続再利用に関連する可能性が高いと判断したため、
            // 毎回新しい接続を張るようにして同種の再発を予防する（この程度の通信頻度では
            // 接続再利用によるレイテンシ削減の恩恵よりも安全性を優先する）。
            .connectionPool(ConnectionPool(0, 1, TimeUnit.NANOSECONDS))
            .build()

        Retrofit.Builder()
            .baseUrl(ApiConfig.BASE_URL)
            .client(client)
            .addConverterFactory(GsonConverterFactory.create())
            .build()
    }

    val photoApiService: PhotoApiService by lazy { retrofit.create(PhotoApiService::class.java) }
    val shogiApiService: ShogiApiService by lazy { retrofit.create(ShogiApiService::class.java) }
}
