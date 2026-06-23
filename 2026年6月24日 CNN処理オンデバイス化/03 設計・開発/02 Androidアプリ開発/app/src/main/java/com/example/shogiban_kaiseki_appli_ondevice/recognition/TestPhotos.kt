package com.example.shogiban_kaiseki_appli_ondevice.recognition

import org.opencv.core.Point

/**
 * ベンチマーク用にバンドルした実写真（00 runtime/server/runtime/photosから複製）と、
 * その4隅座標（PCのrecognition.calibrate_from_imageで自動検出済み、order_pointsで
 * TL,TR,BR,BLの順に並べたもの）。どちらも初期配置と完全一致（mismatch_count=0）を
 * 確認済みの写真なので、オンデバイス推論結果の正解（初期配置）が分かっている状態で
 * 比較できる。赤丸自動検出自体は今回のPoCの対象外（要件定義.md参照）。
 */
data class TestPhoto(val assetName: String, val corners: List<Point>)

object TestPhotos {
    val PHOTO_1 = TestPhoto(
        assetName = "test_photo_1.jpg",
        corners = listOf(
            Point(83.54210662841797, 858.3368530273438),
            Point(2127.325439453125, 928.3035278320312),
            Point(2087.2021484375, 2774.86376953125),
            Point(36.45762634277344, 2740.847412109375),
        )
    )
    val PHOTO_2 = TestPhoto(
        assetName = "test_photo_2.jpg",
        corners = listOf(
            Point(62.412498474121094, 862.211669921875),
            Point(2097.3564453125, 922.0484619140625),
            Point(2069.44189453125, 2757.044677734375),
            Point(27.0, 2739.0),
        )
    )
    val ALL = listOf(PHOTO_1, PHOTO_2)

    // どちらの写真も初期配置と一致する（PC側で確認済み）。recognition.board_to_label_grid
    // (shogi.Board())をそのまま書き出した正解グリッド（row,colの並びはpredict_boardの
    // 出力と同じ規約）。オンデバイス推論結果をこれと比較すれば一致/不一致が分かる。
    val EXPECTED_INITIAL_POSITION = listOf(
        listOf("sente_kyo", "empty", "sente_fu", "empty", "empty", "empty", "gote_fu", "empty", "gote_kyo"),
        listOf("sente_kei", "sente_kaku", "sente_fu", "empty", "empty", "empty", "gote_fu", "gote_hi", "gote_kei"),
        listOf("sente_gin", "empty", "sente_fu", "empty", "empty", "empty", "gote_fu", "empty", "gote_gin"),
        listOf("sente_kin", "empty", "sente_fu", "empty", "empty", "empty", "gote_fu", "empty", "gote_kin"),
        listOf("sente_ou", "empty", "sente_fu", "empty", "empty", "empty", "gote_fu", "empty", "gote_ou"),
        listOf("sente_kin", "empty", "sente_fu", "empty", "empty", "empty", "gote_fu", "empty", "gote_kin"),
        listOf("sente_gin", "empty", "sente_fu", "empty", "empty", "empty", "gote_fu", "empty", "gote_gin"),
        listOf("sente_kei", "sente_hi", "sente_fu", "empty", "empty", "empty", "gote_fu", "gote_kaku", "gote_kei"),
        listOf("sente_kyo", "empty", "sente_fu", "empty", "empty", "empty", "gote_fu", "empty", "gote_kyo"),
    )
}
