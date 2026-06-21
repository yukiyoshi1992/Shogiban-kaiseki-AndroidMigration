package com.example.shogiban_kaiseki_appli.board

/**
 * recognition.py の ALL_LABELS と同じラベル文字列 → 確認画面表示用の短い表記への変換。
 * サーバ側 DISP テーブル（前PJ run_realtime.py 由来）と同じ漢字対応。
 */
private val KANJI = mapOf(
    "fu" to "歩", "kyo" to "香", "kei" to "桂", "gin" to "銀", "kin" to "金",
    "kaku" to "角", "hi" to "飛", "ou" to "玉",
    "tokin" to "と", "nari_kyo" to "杏", "nari_kei" to "圭", "nari_gin" to "全",
    "uma" to "馬", "ryu" to "龍",
)

/** "sente_fu" -> "▲歩", "gote_ryu" -> "△龍", "empty" -> "・" */
fun labelToDisplay(label: String): String {
    if (label == "empty") return "・"
    val prefix = if (label.startsWith("sente_")) "▲" else "△"
    val key = label.removePrefix("sente_").removePrefix("gote_")
    return prefix + (KANJI[key] ?: "?")
}
