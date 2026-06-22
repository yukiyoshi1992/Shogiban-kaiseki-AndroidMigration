package com.example.shogiban_kaiseki_appli.gamelist

data class GameSummary(
    val id: String? = null,
    val filename: String? = null
)

data class GameListResponse(
    val games: List<GameSummary>? = null
)

data class GameDetailResponse(
    val id: String? = null,
    val kif: String? = null
)
