package com.example.shogiban_kaiseki_appli.gamelist

import android.content.ClipData
import android.content.ClipboardManager
import android.content.Intent
import android.widget.Toast
import androidx.activity.compose.BackHandler
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
 *
 * 2026-06-22、KENTO連携方式が確定（Discord経由のユーザー判断）：KENTOの棋譜リストAPIは
 * こちら側からインターネットへAPIを公開する必要があり、セキュリティ上望ましくないため
 * 採用しない。代わりに「棋譜をクリップボードにコピー→KENTOサイトを開いて貼り付け」という
 * 手動連携にする、との明確な指示を受けてクリップボードコピーボタンを追加した。
 *
 * 2026-06-23、5回目UAT課題④（対局再開機能）：終局していない棋譜（resumable=true）の
 * 詳細表示に「対局再開」ボタンを追加。押すとonResumeGameでその対局idを呼び出し元
 * （MainActivity）に伝え、キャリブレーション画面（対局再開モード）へ遷移する。
 */
@Composable
fun GameListScreen(
    modifier: Modifier = Modifier,
    onBack: () -> Unit,
    onResumeGame: (gameId: String) -> Unit
) {
    val context = LocalContext.current
    val coroutineScope = rememberCoroutineScope()

    var dateFilter by remember { mutableStateOf("") }
    var games by remember { mutableStateOf<List<GameSummary>>(emptyList()) }
    var selectedGame by remember { mutableStateOf<GameSummary?>(null) }
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

    // 2026-06-22、3回目UAT課題②：「戻るボタンがなく、スマホの戻るボタンも効かず、
    // アプリを強制終了するしかない」という報告への対応。実際にはボタン自体は存在していたが、
    // 3つのボタンを横一列（Row）に並べていたため、ラベルの長さの合計が画面幅を超えて
    // 右端の「一覧に戻る」が画面外に出てしまい押せなくなっていた——Rowは折り返さず、
    // 画面外にあふれた分はそのままレイアウトされるため気付きにくい不具合だった。
    // Columnに変更し各ボタンを画面幅いっぱいで縦に並べて確実に押せるようにした。
    // 加えてハードウェア（スマホ本体）の戻るボタンは、このアプリが独自のcompose内画面遷移
    // （AppScreen sealed class）を使っており、Androidの標準バックスタックに乗っていないため
    // 何もしていなかった——BackHandlerで明示的にこの画面用の「戻る」相当の処理を割り当てる。
    if (selectedKif != null) {
        BackHandler { selectedKif = null; selectedGame = null }
        Column(modifier = modifier.fillMaxSize().padding(16.dp)) {
            Text(text = selectedKif ?: "", modifier = Modifier.weight(1f).verticalScroll(rememberScrollState()))
            Button(onClick = { selectedKif = null; selectedGame = null }, modifier = Modifier.fillMaxWidth()) { Text("一覧に戻る") }
            Button(onClick = {
                val intent = Intent(Intent.ACTION_SEND).apply {
                    type = "text/plain"
                    putExtra(Intent.EXTRA_TEXT, selectedKif)
                }
                context.startActivity(Intent.createChooser(intent, "KIFを共有"))
            }, modifier = Modifier.fillMaxWidth()) { Text("共有（LINE/メール）") }
            // 2026-06-22、KENTO連携用：KENTO自体へのAPI連携は見送り、コピー→KENTOサイトで
            // 貼り付けという手動連携にするとの指示を受けて追加。ボタン名称は4回目UAT課題②で
            // 「コピー（KENTO用）」→「コピー（分析Tool貼付用）」に変更（KENTO以外の分析ツールにも
            // 貼り付けて使う想定であることが分かりやすいように）。
            Button(onClick = {
                val clipboard = context.getSystemService(ClipboardManager::class.java)
                clipboard.setPrimaryClip(ClipData.newPlainText("KIF", selectedKif))
                Toast.makeText(context, "棋譜をクリップボードにコピーしました", Toast.LENGTH_SHORT).show()
            }, modifier = Modifier.fillMaxWidth()) { Text("コピー（分析Tool貼付用）") }
            // 5回目UAT課題④：終局していない棋譜（resumable）のみ「対局再開」を表示。
            val gameId = selectedGame?.id
            if (selectedGame?.resumable == true && gameId != null) {
                Button(onClick = { onResumeGame(gameId) }, modifier = Modifier.fillMaxWidth()) { Text("対局再開") }
            }
        }
        return
    }

    BackHandler { onBack() }

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
                                    selectedGame = game
                                } catch (e: Exception) {
                                    statusMessage = "取得失敗: ${e.message}"
                                }
                            }
                        }
                    ) {
                        // 2026-06-22、3回目UAT課題③：一覧の見分けがつかないとの指摘への対応。
                        // game.id（プレフィックスを除いた安定識別子、API呼び出し用）ではなく、
                        // 前PJと同じ命名規則の実ファイル名（【対局完了】等の状態表示付き）を表示する。
                        Text(text = game.filename?.removeSuffix(".kif") ?: "(unknown)")
                    }
                }
            }
        }
    }
}
