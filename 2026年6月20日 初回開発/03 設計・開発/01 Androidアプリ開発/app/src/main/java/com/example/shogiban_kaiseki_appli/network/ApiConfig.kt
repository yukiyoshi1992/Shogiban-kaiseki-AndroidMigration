package com.example.shogiban_kaiseki_appli.network

/**
 * サーバのLAN IPアドレスをここに設定する。
 * PC側で `ipconfig` の「IPv4 アドレス」を確認し、末尾のIPだけ書き換える。
 */
object ApiConfig {
    const val BASE_URL = "http://192.168.1.100:8000/"
}
