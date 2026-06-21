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
            .connectTimeout(10, TimeUnit.SECONDS)
            // 2026-06-21、実機テストで盤面写真(2〜3.5MB、壁の写真の0.9MBより大幅に大きい)の
            // キャリブレーション通信でタイムアウトが発生。サーバ側の処理は2〜3秒で完了している
            // ことを実測済みなので、原因はアップロードにかかる時間と判断し、read/writeを伸ばした。
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
