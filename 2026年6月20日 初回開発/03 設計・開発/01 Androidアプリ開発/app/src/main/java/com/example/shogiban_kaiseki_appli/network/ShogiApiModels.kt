package com.example.shogiban_kaiseki_appli.network

/** サーバの /calibration/photo レスポンス。recognizedは9x9のラベル文字列グリッド（行=row,列=col）。 */
data class CalibrationPhotoResponse(
    val status: String? = null,
    val recognized: List<List<String>>? = null
)

data class CalibrationConfirmResponse(
    val status: String? = null,
    val game_id: String? = null
)

/**
 * /move レスポンス。statusは "move" | "nochange" | "error"。
 * move時はmoves/speech_text/move_countが入る。error時はdetailにデバッグ情報が入る。
 */
data class MoveResponse(
    val status: String? = null,
    val moves: List<String>? = null,
    val speech_text: String? = null,
    val move_count: Int? = null,
    val detail: Map<String, Any?>? = null
)

data class GameEndResponse(
    val status: String? = null,
    val game_id: String? = null,
    val move_count: Int? = null
)
