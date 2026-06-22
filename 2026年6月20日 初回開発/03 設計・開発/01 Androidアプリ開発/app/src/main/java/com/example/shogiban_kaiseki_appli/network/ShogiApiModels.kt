package com.example.shogiban_kaiseki_appli.network

/**
 * サーバの /calibration/photo レスポンス。recognizedは9x9のラベル文字列グリッド（行=row,列=col）。
 * matches_initialは認識結果が将棋の初期配置と一致するかどうかをサーバ側で自動判定した結果
 * （2026-06-21、実機テストでのユーザー指摘により人間の目視確認UIから自動判定に変更）。
 * 個別マス補正はしない方針のため、不一致の詳細は件数のみ（mismatch_count）で十分。
 * statusが"playing"なら対局開始済み（game_idが入る）、"ready"なら自動検出が不一致で
 * 対局未開始（2026-06-22、旧/calibration/confirmをこのレスポンスに統合したため、
 * 対局開始の有無もここで分かる）。statusが"pending_confirm"なら手動タップ後の
 * グリッド確認待ち（grid_overlay_jpeg_base64に緑線オーバーレイ画像が入る、UAT課題②）。
 */
data class CalibrationPhotoResponse(
    val status: String? = null,
    val game_id: String? = null,
    val recognized: List<List<String>>? = null,
    val matches_initial: Boolean? = null,
    val mismatch_count: Int? = null,
    val reason: String? = null,
    val grid_overlay_jpeg_base64: String? = null
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
