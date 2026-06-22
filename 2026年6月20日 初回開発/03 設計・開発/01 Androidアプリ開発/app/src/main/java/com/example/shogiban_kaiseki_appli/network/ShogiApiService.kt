package com.example.shogiban_kaiseki_appli.network

import okhttp3.MultipartBody
import okhttp3.RequestBody
import retrofit2.Response
import retrofit2.http.Multipart
import retrofit2.http.POST
import retrofit2.http.Part

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

    @POST("game/abort")
    suspend fun gameAbort(): Response<GameEndResponse>
}
