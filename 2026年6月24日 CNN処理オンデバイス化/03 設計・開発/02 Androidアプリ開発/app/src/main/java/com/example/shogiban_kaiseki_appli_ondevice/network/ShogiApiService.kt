package com.example.shogiban_kaiseki_appli_ondevice.network

import okhttp3.MultipartBody
import okhttp3.RequestBody
import retrofit2.Response
import retrofit2.http.Multipart
import retrofit2.http.POST
import retrofit2.http.Part

/**
 * オンデバイス比較ベンチマーク用に、本番アプリのShogiApiServiceから
 * calibrationPhotoだけを切り出したもの（このPoCでは対局フロー全体は使わない）。
 */
interface ShogiApiService {
    @Multipart
    @POST("calibration/photo")
    suspend fun calibrationPhoto(
        @Part file: MultipartBody.Part,
        @Part("points") points: RequestBody? = null
    ): Response<CalibrationPhotoResponse>
}
