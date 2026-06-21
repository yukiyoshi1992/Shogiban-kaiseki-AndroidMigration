package com.example.shogiban_kaiseki_appli.gamelist

import retrofit2.Response
import retrofit2.http.GET
import retrofit2.http.Path
import retrofit2.http.Query

interface GameListApiService {
    @GET("games")
    suspend fun listGames(@Query("date") date: String? = null): Response<GameListResponse>

    @GET("games/{id}")
    suspend fun getGame(@Path("id") id: String): Response<GameDetailResponse>
}
