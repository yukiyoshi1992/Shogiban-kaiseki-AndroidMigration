package com.example.shogiban_kaiseki_appli_ondevice.network

/**
 * サーバの /calibration/photo レスポンス。recognizedは9x9のラベル文字列グリッド（行=row,列=col）。
 * matches_targetは認識結果が比較対象（新規対局なら初期配置、対局再開ならKIFから再現した
 * 盤面）と一致するかどうかをサーバ側で自動判定した結果（2026-06-21、実機テストでの
 * ユーザー指摘により人間の目視確認UIから自動判定に変更。2026-06-23、5回目UAT課題④の
 * 対局再開機能で比較対象が初期配置に限らなくなったため`matches_initial`から改名）。
 * 個別マス補正はしない方針のため、不一致の詳細は件数のみ（mismatch_count）で十分。
 * statusが"calibration_failed"なら赤丸自動検出に失敗（手動タップへフォールバック）。
 * "pending_confirm"なら対局開始前のグリッド確認待ち（grid_overlay_jpeg_base64に緑線
 * オーバーレイ画像が入る、UAT課題②）——5回目UAT課題④により、不一致（matches_target=false）
 * でも常にここに来る（以前は自動検出の不一致は即・手動タップへフォールバックしていたが、
 * 人間に見せて「このまま進める／手動タップで直す」を選べるように変更）。
 */
data class CalibrationPhotoResponse(
    val status: String? = null,
    val game_id: String? = null,
    val recognized: List<List<String>>? = null,
    val matches_target: Boolean? = null,
    val mismatch_count: Int? = null,
    val reason: String? = null,
    val grid_overlay_jpeg_base64: String? = null
)
