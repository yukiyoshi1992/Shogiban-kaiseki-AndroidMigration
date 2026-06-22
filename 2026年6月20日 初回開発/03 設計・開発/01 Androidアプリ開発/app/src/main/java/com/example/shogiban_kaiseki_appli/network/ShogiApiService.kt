package com.example.shogiban_kaiseki_appli.network

import okhttp3.MultipartBody
import okhttp3.RequestBody
import retrofit2.Response
import retrofit2.http.Multipart
import retrofit2.http.POST
import retrofit2.http.Part
import retrofit2.http.Query

interface ShogiApiService {
    @Multipart
    @POST("calibration/photo")
    suspend fun calibrationPhoto(
        @Part file: MultipartBody.Part,
        @Part("points") points: RequestBody? = null
    ): Response<CalibrationPhotoResponse>

    @POST("calibration/confirm_grid")
    suspend fun calibrationConfirmGrid(): Response<GameEndResponse>

    @Multipart
    @POST("move")
    suspend fun move(@Part file: MultipartBody.Part): Response<MoveResponse>

    @POST("game/end")
    suspend fun gameEnd(): Response<GameEndResponse>

    // 2026-06-22、3回目UAT課題③：KIFファイル名の状態表示（【対局中止】/【対局エラー中止】）を
    // サーバ側で区別するため、ユーザー操作による中止か自動中止かをreasonで伝える
    // （省略時はサーバ側で"user"扱い）。
    @POST("game/abort")
    suspend fun gameAbort(@Query("reason") reason: String? = null): Response<GameEndResponse>
}
