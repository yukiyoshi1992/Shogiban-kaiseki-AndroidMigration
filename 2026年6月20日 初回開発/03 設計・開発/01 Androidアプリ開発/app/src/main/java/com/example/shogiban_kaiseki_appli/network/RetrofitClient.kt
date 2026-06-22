package com.example.shogiban_kaiseki_appli.network

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
