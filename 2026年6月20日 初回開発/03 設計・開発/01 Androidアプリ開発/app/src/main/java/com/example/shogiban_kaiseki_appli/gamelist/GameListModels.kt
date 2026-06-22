package com.example.shogiban_kaiseki_appli.gamelist

data class GameSummary(
    val id: String? = null,
    val filename: String? = null,
    // 5回目UAT課題④（対局再開機能）：終局していない棋譜（【対局完了】以外）かどうか。
    val resumable: Boolean? = null
)

data class GameListResponse(
    val games: List<GameSummary>? = null
)

data class GameDetailResponse(
    val id: String? = null,
    val kif: String? = null
)
