package com.example.shogiban_kaiseki_appli.network

import okhttp3.MultipartBody
import retrofit2.Response
import retrofit2.http.Multipart
import retrofit2.http.POST
import retrofit2.http.Part

data class PhotoUploadResponse(
    val status: String? = null,
    val saved_as: String? = null
)

interface PhotoApiService {
    @Multipart
    @POST("photo")
    suspend fun uploadPhoto(@Part file: MultipartBody.Part): Response<PhotoUploadResponse>
}
