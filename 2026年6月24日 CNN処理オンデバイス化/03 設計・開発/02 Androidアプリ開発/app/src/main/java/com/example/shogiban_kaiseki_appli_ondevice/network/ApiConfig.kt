package com.example.shogiban_kaiseki_appli_ondevice.network

/**
 * サーバのLAN IPアドレスをここに設定する。
 * PC側で `ipconfig` の「IPv4 アドレス」を確認し、末尾のIPだけ書き換える。
 *
 * 注意：本番（00 runtime/server）と同じ8000番ポートではなく、ベンチマーク専用に
 * 別ポート（8001番）で起動した同じサーバーコードを指す。本番の対局セッション状態
 * （session.pyのシングルトン）に影響を与えないため。起動方法：
 * `00 runtime/server`で`uvicorn main:app --host 0.0.0.0 --port 8001`
 * （既存コードは一切変更せず、同じコードをもう一つ起動するだけ）。
 */
object ApiConfig {
    const val BASE_URL = "http://192.168.0.24:8001/"
}
