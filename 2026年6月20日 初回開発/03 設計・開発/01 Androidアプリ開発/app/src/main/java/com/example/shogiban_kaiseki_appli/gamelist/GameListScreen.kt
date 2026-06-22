package com.example.shogiban_kaiseki_appli.gamelist

import android.content.Intent
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.unit.dp
import com.example.shogiban_kaiseki_appli.network.RetrofitClient
import kotlinx.coroutines.launch

/**
 * KIF一覧・共有画面（アプリ要件「②結果共有時」）。
 * GET /games（日付フィルタ）・GET /games/{id} を呼び、対局を選んでKIFを表示・共有する。
 * 共有はAndroid標準の共有シート（LINE/メール等、ACTION_SEND）のみ——KENTO連携（今後の
 * 検討事項No.1）は引き渡し方式（API/ファイル/URL、アプリ内WebView/外部ブラウザ）が
 * まだ未決定のため意図的に未実装。個別KIFの削除・編集機能も要件に記載がないため未実装。
 *
 * 2026-06-22、`@draft_kif_list/`（2026-06-21作成、ビルド対象外で待機していた試作）を
 * 本実装に格上げして統合。試作版が独自に構築していたRetrofitインスタンス
 * （private object GameListRetrofit）は削除し、network/RetrofitClient.ktの
 * gameListApiServiceを使うよう変更（OkHttpClientの設定を1か所にまとめるため、
 * integration_note.txtの「本実装に格上げする場合」の指示通り）。
 */
@Composable
fun GameListScreen(modifier: Modifier = Modifier, onBack: () -> Unit) {
    val context = LocalContext.current
    val coroutineScope = rememberCoroutineScope()

    var dateFilter by remember { mutableStateOf("") }
    var games by remember { mutableStateOf<List<GameSummary>>(emptyList()) }
    var selectedKif by remember { mutableStateOf<String?>(null) }
    var statusMessage by remember { mutableStateOf("") }

    fun reload() {
        coroutineScope.launch {
            try {
                val response = RetrofitClient.gameListApiService.listGames(dateFilter.ifBlank { null })
                games = response.body()?.games ?: emptyList()
                statusMessage = if (games.isEmpty()) "対局が見つかりません" else ""
            } catch (e: Exception) {
                statusMessage = "取得失敗: ${e.message}"
            }
        }
    }

    LaunchedEffect(Unit) { reload() }

    if (selectedKif != null) {
        Column(modifier = modifier.fillMaxSize().padding(16.dp)) {
            Text(text = selectedKif ?: "", modifier = Modifier.weight(1f).verticalScroll(rememberScrollState()))
            Row(horizontalArrangement = Arrangement.SpaceEvenly, modifier = Modifier.fillMaxWidth()) {
                Button(onClick = {
                    val intent = Intent(Intent.ACTION_SEND).apply {
                        type = "text/plain"
                        putExtra(Intent.EXTRA_TEXT, selectedKif)
                    }
                    context.startActivity(Intent.createChooser(intent, "KIFを共有"))
                }) { Text("共有（LINE/メール）") }
                Button(onClick = { selectedKif = null }) { Text("一覧に戻る") }
            }
        }
        return
    }

    Column(modifier = modifier.fillMaxSize().padding(16.dp)) {
        Row(modifier = Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceEvenly) {
            Button(onClick = onBack) { Text("戻る") }
            OutlinedTextField(
                value = dateFilter,
                onValueChange = { dateFilter = it },
                label = { Text("日付(YYYYMMDD、空欄で全件)") },
                modifier = Modifier.weight(1f)
            )
            Button(onClick = { reload() }) { Text("検索") }
        }
        if (statusMessage.isNotEmpty()) {
            Text(text = statusMessage, modifier = Modifier.padding(8.dp))
        }
        LazyColumn(modifier = Modifier.fillMaxSize()) {
            items(games) { game ->
                Box(
                    modifier = Modifier.fillMaxWidth().padding(8.dp)
                ) {
                    Button(
                        modifier = Modifier.fillMaxWidth(),
                        onClick = {
                            val id = game.id ?: return@Button
                            coroutineScope.launch {
                                try {
                                    val response = RetrofitClient.gameListApiService.getGame(id)
                                    selectedKif = response.body()?.kif ?: "(取得失敗)"
                                } catch (e: Exception) {
                                    statusMessage = "取得失敗: ${e.message}"
                                }
                            }
                        }
                    ) {
                        Text(text = game.id ?: "(unknown)")
                    }
                }
            }
        }
    }
}
