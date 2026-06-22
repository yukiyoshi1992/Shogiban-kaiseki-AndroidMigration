"""APIサーバ本体。

`/health` と `/photo`（写真を受け取って保存するだけの通信確認用ダミー）は初期動作確認で使った
ものをそのまま残してある。本番の対局フローは `/calibration/photo` 以降の新エンドポイント。

エンドポイント構成はアーキテクチャ検討.md 3節の案がベースだが、2026-06-22に
/calibration/confirmは廃止・統合済み（旧人間確認UIの残骸だったため）。代わりに
グリッド確認専用の/calibration/confirm_gridを追加（UAT課題②、下記参照）:
  POST /calibration/photo        キャリブレーション写真（自動検出 or 4隅タップ）→ 認識結果。
                                  「採用される可能性のある」キャリブレーション
                                  （4隅タップは常に、自動検出は初期配置と一致した場合のみ）は、
                                  対局開始の前にグリッド線重畫画像（緑線）を返し、人間が目視確認
                                  する（status="pending_confirm"。2回目UAT課題③で自動検出側にも
                                  適用範囲を拡大）。自動検出が初期配置と不一致の場合は人間の
                                  手動タップへフォールバックする（status="ready"、対局開始もしない）。
  POST /calibration/confirm_grid 上記の目視確認でOKだった場合のみ呼ぶ→対局開始。
  POST /move                     1手分の写真 → classify_frameで判定 → KIF追記+読み上げテキスト
  POST /game/end                 終局通知 → KIF確定、状態をidleに戻す
  POST /game/abort                対局中止 → 状態を無条件でidleに戻す（テスト用、UAT課題①）
  GET  /games                    KIF一覧（開始日付で絞り込み可）
  GET  /games/{game_id}          指定KIFの内容取得

盤面認識・指し手判定ロジックは recognition.py、対局状態は session.py のシングルトンに分離。
"""

import base64
import json
import re
import time
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np
import shogi
from fastapi import FastAPI, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse

import recognition
from session import GameState, session

app = FastAPI()

BASE_DIR = Path(__file__).parent
MODEL_DIR = BASE_DIR / "models"
RUNTIME_DIR = BASE_DIR / "runtime"
RUNTIME_PHOTOS_DIR = RUNTIME_DIR / "photos"
RUNTIME_GAMES_DIR = RUNTIME_DIR / "games"
RUNTIME_PHOTOS_DIR.mkdir(parents=True, exist_ok=True)
RUNTIME_GAMES_DIR.mkdir(parents=True, exist_ok=True)

# 2026-06-22、タイムアウト調査用：[timing]ログをコンソールだけでなくファイルにも残す
# （ユーザーにコンソールのコピペを頼まず、claude code側で直接読んで分析できるようにするため）。
SERVER_LOG_PATH = RUNTIME_DIR / "server_timing.log"

# 2026-06-22、3回目UAT課題③：KIFファイル名を前PJ（画面要件.xlsx「画面イメージ」シートO7セル、
# app_streamlit.py kif_filename_for_start/rename_kif_prefix）と同じ命名規則に統一。
# 仕様：【対局中】yyyyMMdd_hh-mm.kif（":"はWindowsファイル名に使えないため前PJ同様"-"に変更済み）。
# 対局終了/中止/エラー中止のタイミングで【】内の状態表示だけを付け替える（日時部分は対局開始時刻で
# 固定）。session.game_idにはこの日時部分（プレフィックスを除いた安定識別子）だけを保持する——
# 状態遷移でファイル名（＝プレフィックス）が変わっても、game_idそのものは変わらないようにするため
# （/games/{game_id}での検索はプレフィックスを無視してこのid部分で行う）。
KIF_PREFIX_PLAYING = "【対局中】"
KIF_PREFIX_FINISHED = "【対局完了】"
KIF_PREFIX_ABORTED = "【対局中止】"
KIF_PREFIX_ERROR_ABORTED = "【対局エラー中止】"


def _strip_kif_prefix(stem: str) -> str:
    """ファイル名から先頭の【...】状態表示を取り除き、安定識別子部分だけを返す。
    旧形式（game_YYYYMMDD_HHMMSS、プレフィックスなし）はそのまま返す。"""
    return re.sub(r"^【[^】]*】", "", stem)


def _rename_kif(new_prefix: str) -> None:
    """対局中のKIFファイルの状態プレフィックスを付け替える。/game/abortはどの状態からでも
    無条件で呼べる設計のため、対局がまだ始まっていない（kif_path未設定）場合は何もしない。"""
    if session.kif_path is None or not session.kif_path.exists():
        return
    new_path = session.kif_path.with_name(f"{new_prefix}{session.game_id}.kif")
    session.kif_path.rename(new_path)
    session.kif_path = new_path


def _log(msg: str) -> None:
    print(msg, flush=True)
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    with open(SERVER_LOG_PATH, "a", encoding="utf-8") as f:
        f.write(f"[{ts}] {msg}\n")

# 旧来の通信確認用ダミーエンドポイント（/photo）が使っていた保存先。本番フローは runtime/ 配下を使う。
UPLOAD_DIR = BASE_DIR / "received_photos"
UPLOAD_DIR.mkdir(exist_ok=True)

print("モデル読み込み中...")
MODEL = recognition.load_model(MODEL_DIR)
print("モデル読み込み完了。")

# 2026-06-22、3回目UAT課題①：サーバ再起動後、最初の1回目のpredict_board()だけ
# 異常に遅い（実測17.7秒、通常時1.3〜1.9秒の約10倍）→ひどい場合は数分単位で
# ハングしたように見え、その間に来た別リクエストも応答が返らず、クライアント側は
# 接続タイムアウト（10秒）で諦めてしまう、という事象がserver_timing.logの記録
# （19:54:52・19:54:58受信→完了ログなし、19:56台でようやく応答再開）と一致した。
# 原因はPyTorch CPU推論特有の「初回フォワードパスだけ遅い」現象（スレッドプール・
# カーネル選択等の初期化が初回に発生するため）と推測——モデル自体は起動時に
# 読み込み済み（load_state_dict）なので、ファイルI/Oの遅さではない。
# 対策：起動時にダミー画像で1回だけ推論しておき、初回コストを「サーバ起動時」に
# 払わせる（誰も待っていないタイミングに移す）。ユーザーの最初の本番撮影が
# 遅くならないようにするのが目的で、推論結果自体は使わない。
print("モデルのウォームアップ中（初回推論コストをここで払う）...")
_warmup_start = time.perf_counter()
recognition.predict_board(MODEL, np.zeros((recognition.WARP_SIDE, recognition.WARP_SIDE, 3), dtype=np.uint8))
print(f"ウォームアップ完了（{time.perf_counter() - _warmup_start:.2f}秒）。")


def _decode_image(data: bytes):
    arr = np.frombuffer(data, dtype=np.uint8)
    return cv2.imdecode(arr, cv2.IMREAD_COLOR)


def _save_runtime_photo(img, prefix: str) -> Path:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    path = RUNTIME_PHOTOS_DIR / f"{prefix}_{timestamp}.jpg"
    cv2.imencode(".jpg", img)[1].tofile(str(path))
    return path


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/photo")
async def receive_photo(file: UploadFile) -> dict[str, str]:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = UPLOAD_DIR / f"{timestamp}_{file.filename}"
    dest.write_bytes(await file.read())
    return {"status": "ok", "saved_as": dest.name}


@app.post("/calibration/photo")
async def calibration_photo(file: UploadFile, points: str | None = Form(None)):
    """写真を受け取りキャリブレーションし、必要なら対局開始まで1リクエストで完了する。
    idleからのみ呼べる（失敗・不一致時はidleに留まるので何度でも呼び直せる）。

    pointsを省略すると、まず前PJと同じ赤丸4個の自動検出を試す
    （2026-06-21、「4隅の手動タップが難しすぎる」というユーザー指摘により、
    手動タップが先ではなく自動検出が先になる二段構成に変更）。
    自動検出に失敗した場合は status="calibration_failed" を返すので、アプリ側は
    同じ写真に対して盤の4隅座標を添えてこのエンドポイントを再度呼ぶ（人間のフォールバック）。
    pointsを指定した場合は常にその4点座標（JSON配列 "[[x1,y1],...,[x4,y4]]"、順不同）を使う。

    対局開始の判断（2026-06-22、旧/calibration/confirmを統合。2回目UAT課題③で範囲拡大）：
    手動タップ（points指定）・自動検出が初期配置と完全一致した場合は、いずれも
    「採用される可能性のあるキャリブレーション」として人間にグリッド線確認をさせる
    （status="pending_confirm"。対局開始は/calibration/confirm_gridを待つ）。
    自動検出が不一致の場合は対局を開始せずstatus="ready"を返し、idleに留まる
    （アプリ側は人間の手動タップへフォールバックする）。
    旧設計では「9x9を人間が目視確認してOKを押す」ための別エンドポイント
    （/calibration/confirm）への2回目のリクエストが必要だったが、その人間確認UIは
    自動判定（matches_initial）に置き換えられて廃止済みで、2回目のリクエストに
    人間の判断は何も介在しなくなっていた。かつこの2回目のリクエストが実機で
    断続的にタイムアウトする事象が確認されたため、両エンドポイントを統合し
    2回目の通信自体をなくした。UAT課題②でグリッド確認（タップ精度の目視確認）として
    2回目の通信が復活したが、今度は人間の判断が実際に介在するので同種のタイムアウト
    再発リスクは小さいと判断（RetrofitClientのConnectionPool設定も対策済み）。
    """
    t_start = time.perf_counter()
    _log(f"[calibration/photo] request received, mode={'auto' if points is None else 'manual'}")
    body = await file.read()
    t_body_read = time.perf_counter()
    img = _decode_image(body)
    if img is None:
        raise HTTPException(400, "failed to decode image")
    t_decode = time.perf_counter()

    # 状態チェックより前に必ず保存する（赤丸検出が失敗するケースほど原因調査に写真が必要なため。
    # 2026-06-21、状態チェックの409エラーが保存より先に発生し写真が一切残らない回帰が発生したため、
    # 保存を状態チェックより前に移動した）。
    _save_runtime_photo(img, "calib")
    t_save = time.perf_counter()

    if session.state != GameState.IDLE:
        raise HTTPException(409, f"calibration not allowed in state={session.state.value}")

    if points is None:
        matrix = recognition.calibrate_from_image(img)
        t_calib = time.perf_counter()
        if matrix is None:
            _log(f"[timing] body_read={t_body_read-t_start:.2f}s decode={t_decode-t_body_read:.2f}s "
                 f"save={t_save-t_decode:.2f}s calib={t_calib-t_save:.2f}s -> calibration_failed "
                 f"(responding now)")
            return {"status": "calibration_failed", "reason": "red_circles_not_found"}
    else:
        try:
            pts = json.loads(points)
            if len(pts) != 4:
                raise ValueError
        except (json.JSONDecodeError, ValueError, TypeError):
            raise HTTPException(400, "points must be a JSON array of exactly 4 [x, y] pairs")
        ordered = recognition.order_points(pts)
        matrix = recognition.compute_calib(ordered)
        t_calib = time.perf_counter()

    warped = recognition.warp_board(img, matrix)
    recognized = recognition.predict_board(MODEL, warped)
    t_predict = time.perf_counter()
    mismatches = recognition.compare_to_initial(recognized)

    # 「採用される可能性のあるキャリブレーション」はpending_confirm（グリッド線オーバーレイを
    # 返し、人間の目視確認後に/calibration/confirm_gridで対局開始）に進む：
    # - 手動タップ（points指定）は常に（前PJ方針通り無条件採用、タップ精度の確認のみ挟む）
    # - 自動検出は初期配置と完全一致した場合のみ（2026-06-22、2回目UAT課題③で追加。
    #   従来は自動検出の一致判定＝盤面認識の正しさの確認だけで対局を即時開始していたが、
    #   それは「4隅タップ自体の精度（透視変換が盤に正しく合っているか）」の確認ではないため、
    #   手動タップと同じグリッド確認をくぐらせるよう統一した）。
    # 自動検出が初期配置と不一致の場合のみpending_confirmに進まず、ready（idle留まり、
    # 人間の手動タップへフォールバック）のまま。
    pending = points is not None or len(mismatches) == 0
    t_end = time.perf_counter()
    if pending:
        session.pending_calib_matrix = matrix
        overlay_b64 = base64.b64encode(recognition.grid_overlay_jpeg_bytes(img, matrix)).decode("ascii")
        _log(f"[timing] body_read={t_body_read-t_start:.2f}s decode={t_decode-t_body_read:.2f}s "
             f"save={t_save-t_decode:.2f}s calib={t_calib-t_save:.2f}s predict={t_predict-t_calib:.2f}s "
             f"total={t_end-t_start:.2f}s -> pending_confirm "
             f"(responding now, mismatch_count={len(mismatches)})")
        return {
            "status": "pending_confirm",
            "grid_overlay_jpeg_base64": overlay_b64,
            "recognized": recognized,
            "matches_initial": len(mismatches) == 0,
            "mismatch_count": len(mismatches),
        }

    _log(f"[timing] body_read={t_body_read-t_start:.2f}s decode={t_decode-t_body_read:.2f}s "
         f"save={t_save-t_decode:.2f}s calib={t_calib-t_save:.2f}s predict={t_predict-t_calib:.2f}s "
         f"total={t_end-t_start:.2f}s -> ready (auto mismatch, falling back to manual tap) "
         f"(responding now, mismatch_count={len(mismatches)})")
    return {
        "status": "ready",
        "game_id": None,
        "recognized": recognized,
        "matches_initial": False,
        "mismatch_count": len(mismatches),
    }


@app.post("/calibration/confirm_grid")
async def calibration_confirm_grid():
    """グリッド目視確認でOKだった場合に呼ぶ→対局開始。手動タップ（UAT課題②）・
    自動検出成功（2回目UAT課題③）のどちらの後でも同じエンドポイントを使う。
    NGの場合はこのエンドポイントを呼ばず、アプリ側は手動タップ画面に戻るだけでよい
    （pending_calib_matrixは次の/calibration/photoで上書きされるか、対局中止/再起動で
    クリアされる）。
    """
    if session.pending_calib_matrix is None:
        raise HTTPException(409, "no pending calibration to confirm")

    matrix = session.pending_calib_matrix
    session.pending_calib_matrix = None
    session.calib_matrix = matrix
    session.board = shogi.Board()
    session.moves_usi = []
    now = datetime.now()
    session.start_time = now
    session.last_move_time = now
    game_id = now.strftime("%Y%m%d_%H-%M")
    session.game_id = game_id
    session.kif_path = RUNTIME_GAMES_DIR / f"{KIF_PREFIX_PLAYING}{game_id}.kif"
    session.kif_path.write_text(
        f"開始日時：{now.strftime('%Y/%m/%d %H:%M:%S')}\n手数----指手---------消費時間--\n",
        encoding="utf-8",
    )
    session.state = GameState.PLAYING
    return {"status": "playing", "game_id": game_id}


@app.post("/move")
async def post_move(file: UploadFile):
    """1手分の写真を受け取り、classify_frameで指し手を判定する。
    成功時はKIFに1行追記し、読み上げ用テキストを返す。

    失敗時の方針（2026-06-22、2回目UAT課題⑤で確定。前PJ`app_streamlit.py`の
    「画面要件No.6: 認識エラーで続行不能→対局中止と同じ動作を自動実行」を踏襲）：
    撮影のし直し（リトライ）は行わず、アプリ側が`/game/abort`相当の処理を自動実行する
    （誤ったKIFを生成するより諦める方針）。サーバはこの自動中止の判断自体は行わない
    （セッション状態は維持したまま"error"を返すだけ）——アプリ側が受け取って中止する。
    ここまでに記録済みの手数（move_count）も合わせて返すので、アプリ側は前PJの
    「n手目まで記録」のような中断理由表示に使える。
    """
    img = _decode_image(await file.read())
    if img is None:
        raise HTTPException(400, "failed to decode image")

    _save_runtime_photo(img, "move")

    if session.state != GameState.PLAYING:
        raise HTTPException(409, f"move not allowed in state={session.state.value}")

    warped = recognition.warp_board(img, session.calib_matrix)
    recognized = recognition.predict_board(MODEL, warped)
    status, payload = recognition.classify_frame(session.board, recognized)

    if status == "move":
        speech_texts = []
        now = datetime.now()
        # 1リクエストで複数手（depth2）が確定する場合があるので、消費時間は
        # まとめてこのリクエスト分として全手に同じ値を記録する（厳密さは不要な要件のため）
        this_move_seconds = (now - session.last_move_time).total_seconds()
        total_seconds = (now - session.start_time).total_seconds()
        time_field = recognition.kif_time_field(this_move_seconds, total_seconds)
        session.last_move_time = now
        for usi in payload:
            move = shogi.Move.from_usi(usi)
            kif_text, speech_text = recognition.move_to_text(session.board, move)
            session.board.push(move)
            session.moves_usi.append(usi)
            move_number = len(session.moves_usi)
            with open(session.kif_path, "a", encoding="utf-8") as f:
                f.write(f"{move_number:4d} {kif_text} {time_field}\n")
            speech_texts.append(speech_text)
        return {
            "status": "move",
            "moves": payload,
            "speech_text": "、".join(speech_texts),
            "move_count": len(session.moves_usi),
        }

    if status == "nochange":
        return {"status": "nochange"}

    # 2回目UAT課題⑤：前PJ（Streamlit版）の「認識エラー時は対局中止と同じ動作を自動実行」
    # 方針を踏襲するため、アプリ側が中断理由をポップアップ表示できるよう、ここまでに
    # 記録済みの手数も合わせて返す。
    return JSONResponse(status_code=200, content={
        "status": "error", "detail": payload, "move_count": len(session.moves_usi)
    })


@app.post("/game/end")
async def game_end():
    """終局通知。KIFは/moveの時点で逐次追記済みのため、状態をidleに戻すのみだが、
    3回目UAT課題③によりKIFファイル名の状態表示（【対局中】→【対局完了】）も付け替える。
    """
    if session.state != GameState.PLAYING:
        raise HTTPException(409, f"game/end not allowed in state={session.state.value}")

    session.state = GameState.FINISHING
    _rename_kif(KIF_PREFIX_FINISHED)
    result = {
        "status": "idle",
        "game_id": session.game_id,
        "move_count": len(session.moves_usi),
    }
    session.reset()
    return result


@app.post("/game/abort")
async def game_abort(reason: str = "user"):
    """対局中止（2026-06-22、UAT課題①）。/game/endと違い対局開始前提の状態チェックをせず、
    現在どの状態にあっても無条件でidleに戻す——テスト中に「やり直したい」場面で確実に
    使える強制リセットとして使うため（サーバが想定外の状態で固まった場合の回復手段にもなる）。
    KIFファイルは削除せず、記録済みの手数まではそのまま残す。

    2026-06-22、3回目UAT課題③：前PJ同様、ユーザーが押す「中止」と、認識エラー/通信エラーに
    よる自動中止（2回目UAT課題⑤）とでKIFファイル名の状態表示を区別する
    （【対局中止】 / 【対局エラー中止】）。reason="error"はAndroid側の自動中止経路
    （PlayScreen.ktのabortGame呼び出し）から渡される——アプリ強制終了等で届かなかった場合は
    区別がつかないため、その場合は無条件で"user"扱い（無難な方）にフォールバックする。
    """
    prefix = KIF_PREFIX_ERROR_ABORTED if reason == "error" else KIF_PREFIX_ABORTED
    _rename_kif(prefix)
    session.reset()
    return {"status": "idle"}


@app.get("/games")
async def list_games(date: str | None = None):
    """KIF一覧。dateを指定すると対局開始日付（YYYYMMDD or YYYY-MM-DD）で絞り込む。
    3回目UAT課題③：ファイル名が【対局中】等の状態プレフィックス付きになったため、
    日付抽出は固定インデックスではなく「最初に現れる8桁の数字」を正規表現で拾う方式に変更
    （新形式・旧形式game_YYYYMMDD_HHMMSSの両方に対応）。
    """
    date_filter = date.replace("-", "") if date else None
    games = []
    for path in sorted(RUNTIME_GAMES_DIR.glob("*.kif"), reverse=True):
        m = re.search(r"\d{8}", path.stem)
        date_part = m.group(0) if m else ""
        if date_filter and date_part != date_filter:
            continue
        games.append({"id": _strip_kif_prefix(path.stem), "filename": path.name})
    return {"games": games}


@app.get("/games/{game_id}")
async def get_game(game_id: str):
    """3回目UAT課題③：ファイル名の状態プレフィックスが対局終了/中止のタイミングで変わるため、
    idから直接パスを構築できない。プレフィックスを除いた部分が一致するファイルを探す。
    """
    for path in RUNTIME_GAMES_DIR.glob("*.kif"):
        if _strip_kif_prefix(path.stem) == game_id:
            return {"id": game_id, "kif": path.read_text(encoding="utf-8")}
    raise HTTPException(404, "game not found")
