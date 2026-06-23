"""APIサーバ本体。

`/health` と `/photo`（写真を受け取って保存するだけの通信確認用ダミー、初期動作確認用）は
6回目UAT課題で削除済み（`camera/@old/CameraScreen.kt`からしか参照されておらず、その
アーカイブファイル自体も合わせて削除する判断をユーザーから得た）。本番の対局フローは
`/calibration/photo` 以降の新エンドポイントのみ。

エンドポイント構成はアーキテクチャ検討.md 3節の案がベースだが、2026-06-22に
/calibration/confirmは廃止・統合済み（旧人間確認UIの残骸だったため）。代わりに
グリッド確認専用の/calibration/confirm_gridを追加（UAT課題②、下記参照）:
  POST /calibration/photo        キャリブレーション写真（自動検出 or 4隅タップ）→ 認識結果。
                                  矩形変換（matrix）が得られれば常に、対局開始の前に
                                  グリッド線重畫画像（緑線）＋不一致件数を返し、人間が確認する
                                  （status="pending_confirm"）。5回目UAT課題④で「自動検出が
                                  不一致なら即・手動タップへ」という方針を変更し、不一致でも
                                  人間が見て「このまま進める／手動タップで直す」を選べるように
                                  した（resume_game_id省略時は初期配置、指定時は中断局のKIFから
                                  再現した盤面が比較対象）。
  POST /calibration/confirm_grid 上記の確認でOK、または不一致でも進める場合に呼ぶ→対局開始
                                  （対局再開の場合は既存KIFを再開、新規なら新しいKIFを作る）。
  POST /move                     1手分の写真 → classify_frameで判定 → KIF追記+読み上げテキスト
  POST /game/end                 終局通知 → KIF確定、状態をidleに戻す
  POST /game/abort                対局中止 → 状態を無条件でidleに戻す（テスト用、UAT課題①）
  GET  /games                    KIF一覧（開始日付で絞り込み可、resumableで再開対象かも分かる）
  GET  /games/{game_id}          指定KIFの内容取得

盤面認識・指し手判定ロジックは recognition.py、対局状態は session.py のシングルトンに分離。
"""

import base64
import json
import re
import time
from datetime import datetime
from html import escape as html_escape
from pathlib import Path

import cv2
import numpy as np
import shogi
import shogi.KIF
from fastapi import FastAPI, Form, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

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


def _kif_chronological_key(stem: str) -> str:
    """一覧の並び順用：ファイル名から数字（日付＋時刻）だけを取り出したキーを返す。

    6回目UAT課題④：list_games()は以前、生のファイル名（先頭に【対局中止】等の状態
    プレフィックスを含む）をそのままソートキーにしていたため、文字コード上プレフィックス
    文字列ごとにグルーピングされてしまい、対局完了・対局中止が時系列を無視して別々の
    ブロックにまとまって表示されていた。プレフィックスも"game_"等の英字部分もすべて
    数字以外として除去すれば、新形式（yyyyMMdd_hh-mm）・旧形式（game_YYYYMMDD_HHMMSS）
    どちらも日付＋時刻の数字列だけが残り、文字列比較がそのまま時系列比較になる。
    """
    return re.sub(r"\D", "", stem)


def _rename_kif(new_prefix: str) -> None:
    """対局中のKIFファイルの状態プレフィックスを付け替える。/game/abortはどの状態からでも
    無条件で呼べる設計のため、対局がまだ始まっていない（kif_path未設定）場合は何もしない。

    5回目UAT課題④の実装中に発見（テストで同じ分内に複数の対局を開始・中止して再現）：
    game_idは分単位（yyyyMMdd_hh-mm）なので、同じ分に複数回対局を開始・中止すると
    異なる対局同士が同じ名前（【対局中止】等＋同じid）になり得て、renameが
    FileExistsErrorで失敗していた（=対局中止/終局自体が500エラーで失敗し、クライアントは
    終わったと思っているのにサーバ側のセッションが残り続ける——過去に直した409食い違いと
    同種の問題）。衝突したら連番を振って回避する。
    """
    if session.kif_path is None or not session.kif_path.exists():
        return
    base_name = f"{new_prefix}{session.game_id}"
    new_path = session.kif_path.with_name(f"{base_name}.kif")
    suffix = 2
    while new_path.exists() and new_path != session.kif_path:
        new_path = session.kif_path.with_name(f"{base_name}_{suffix}.kif")
        suffix += 1
    session.kif_path.rename(new_path)
    session.kif_path = new_path


# 5回目UAT課題④（対局再開機能）：「終局していない棋譜」＝【対局完了】以外すべて
# （【対局中】＝異常終了等で取り残されたもの、【対局中止】【対局エラー中止】＝ユーザーが
# 明示的に終わらせたもの）を再開対象にする、とユーザー確認済み（対象を絞る理由はないとの判断）。
RESUMABLE_PREFIXES = (KIF_PREFIX_PLAYING, KIF_PREFIX_ABORTED, KIF_PREFIX_ERROR_ABORTED)


def _is_resumable(filename: str) -> bool:
    return any(filename.startswith(p) for p in RESUMABLE_PREFIXES)


def _find_resumable_kif(game_id: str) -> Path | None:
    """安定識別子(game_id)から再開対象のKIFファイルを探す。【対局完了】や、状態表示
    プレフィックスのない旧形式ファイルは対象外（旧形式は本機能より前のテストデータで、
    再開対象かどうかが不明なため安全側に倒して対象外とする）。"""
    for path in RUNTIME_GAMES_DIR.glob("*.kif"):
        if _is_resumable(path.name) and _strip_kif_prefix(path.stem) == game_id:
            return path
    return None


_KIF_START_TIME_RE = re.compile(r"開始日時：(\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2})")


def _load_kif_for_resume(path: Path) -> tuple[list[str], "datetime | None"]:
    """保存済みKIFファイルから指し手の履歴（USI形式）と対局開始時刻を復元する。
    開始日時ヘッダーは②対応（2026-06-23）より前のKIFには存在しないため、見つからなければ
    Noneを返す（呼び出し側は現在時刻にフォールバックする）。"""
    parsed = shogi.KIF.Parser.parse_file(str(path))[0]
    moves = list(parsed["moves"])
    text = path.read_text(encoding="utf-8")
    m = _KIF_START_TIME_RE.search(text)
    start_time = datetime.strptime(m.group(1), "%Y/%m/%d %H:%M:%S") if m else None
    return moves, start_time


def _log(msg: str) -> None:
    print(msg, flush=True)
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    with open(SERVER_LOG_PATH, "a", encoding="utf-8") as f:
        f.write(f"[{ts}] {msg}\n")

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


@app.post("/calibration/photo")
async def calibration_photo(
    file: UploadFile,
    points: str | None = Form(None),
    resume_game_id: str | None = Form(None),
):
    """写真を受け取りキャリブレーションし、グリッド確認待ち（pending_confirm）にする。
    idleからのみ呼べる（失敗・確認待ちのままなら何度でも呼び直せる）。

    pointsを省略すると、まず前PJと同じ赤丸4個の自動検出を試す
    （2026-06-21、「4隅の手動タップが難しすぎる」というユーザー指摘により、
    手動タップが先ではなく自動検出が先になる二段構成に変更）。
    自動検出に失敗した場合は status="calibration_failed" を返すので、アプリ側は
    同じ写真に対して盤の4隅座標を添えてこのエンドポイントを再度呼ぶ（人間のフォールバック）。
    pointsを指定した場合は常にその4点座標（JSON配列 "[[x1,y1],...,[x4,y4]]"、順不同）を使う。

    resume_game_idを指定すると、新規対局（初期配置と比較）ではなく中断局の再開
    （5回目UAT課題④）として扱う——比較対象はそのKIFファイルから再現した盤面になる。
    省略時（通常の新規対局）は初期配置（shogi.Board()）と比較する。

    対局開始の判断（2026-06-23、5回目UAT課題④で「不一致時は即・手動タップへ」方針を変更）：
    赤丸自動検出・手動タップのいずれでも、矩形変換（matrix）が得られた時点で常に
    pending_confirm（グリッド線オーバーレイ＋認識結果の不一致件数）を返す。以前は
    自動検出が比較対象と不一致の場合、人間に見せずに即・手動タップへフォールバックして
    いたが、ユーザーから「不一致でも内容を見せて、このまま進めるか手動で直すか選べる
    ようにしたい（駒の並べ間違いに気づけたケースがあったため）」との明確な指示を
    受けて変更した。手動タップ側はこれまで通り「無条件採用」方針（個別マス補正はしない）
    のまま変更なし——pending_confirmのmismatch_countは手動タップでは表示用の参考情報
    （アプリ側は従来通りOK/やり直すの2択のみを出す）。
    不一致のまま人間が「このまま進める」を選んだ場合に備え、認識結果から復元した
    Boardも`pending_board`としてこの時点で計算・保持しておく
    （`recognition.label_grid_to_board`、confirm_grid側は分岐を持たず常に
    `pending_board`を使うだけで済む）。
    """
    t_start = time.perf_counter()
    _log(f"[calibration/photo] request received, mode={'auto' if points is None else 'manual'}"
         f"{f', resume={resume_game_id}' if resume_game_id else ''}")
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

    resume_kif_path: Path | None = None
    resume_moves: list[str] = []
    resume_start_time = None
    if resume_game_id is not None:
        resume_kif_path = _find_resumable_kif(resume_game_id)
        if resume_kif_path is None:
            raise HTTPException(404, f"resumable game not found: {resume_game_id}")
        resume_moves, resume_start_time = _load_kif_for_resume(resume_kif_path)

    target_board = shogi.Board()
    for usi in resume_moves:
        target_board.push_usi(usi)

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
    mismatches = recognition.compare_to_board(recognized, target_board)
    pending_board = (
        target_board if len(mismatches) == 0
        else recognition.label_grid_to_board(recognized, target_board)
    )

    session.pending_calib_matrix = matrix
    session.pending_board = pending_board
    session.pending_resume_kif_path = resume_kif_path
    session.pending_resume_moves = resume_moves
    session.pending_resume_start_time = resume_start_time
    session.pending_resume_game_id = resume_game_id

    overlay_b64 = base64.b64encode(
        recognition.grid_overlay_jpeg_bytes(img, matrix, mismatches=mismatches)
    ).decode("ascii")
    t_end = time.perf_counter()
    _log(f"[timing] body_read={t_body_read-t_start:.2f}s decode={t_decode-t_body_read:.2f}s "
         f"save={t_save-t_decode:.2f}s calib={t_calib-t_save:.2f}s predict={t_predict-t_calib:.2f}s "
         f"total={t_end-t_start:.2f}s -> pending_confirm "
         f"(responding now, mismatch_count={len(mismatches)})")
    return {
        "status": "pending_confirm",
        "grid_overlay_jpeg_base64": overlay_b64,
        "recognized": recognized,
        "matches_target": len(mismatches) == 0,
        "mismatch_count": len(mismatches),
    }


@app.post("/calibration/confirm_grid")
async def calibration_confirm_grid():
    """グリッド確認でOK、または不一致でも「このまま進める」を選んだ場合に呼ぶ→対局開始
    （5回目UAT課題④で、不一致でも進められるよう拡張）。手動タップ・自動検出成功・
    自動検出不一致のいずれの後でも同じエンドポイントを使う。NGの場合はこのエンドポイントを
    呼ばず、アプリ側は手動タップ画面に戻るだけでよい（pending_*は次の/calibration/photoで
    上書きされるか、対局中止/再起動でクリアされる）。

    `session.pending_resume_kif_path`が設定されていれば対局再開——既存のKIFファイルの
    状態表示を【対局中】に戻し、指し手履歴・対局開始時刻を引き継ぐ。未設定なら新規対局
    として新しいKIFファイルを作る。いずれも`session.board`には（一致していればそのまま、
    不一致でも人間が進めることを選んだ場合は認識結果から復元した）`pending_board`を使う
    ——以後の指し手判定はこのBoardを正として行われる。
    """
    if session.pending_calib_matrix is None or session.pending_board is None:
        raise HTTPException(409, "no pending calibration to confirm")

    matrix = session.pending_calib_matrix
    board = session.pending_board
    resume_kif_path = session.pending_resume_kif_path
    resume_moves = session.pending_resume_moves
    resume_start_time = session.pending_resume_start_time
    resume_game_id = session.pending_resume_game_id
    session.pending_calib_matrix = None
    session.pending_board = None
    session.pending_resume_kif_path = None
    session.pending_resume_moves = []
    session.pending_resume_start_time = None
    session.pending_resume_game_id = None

    session.calib_matrix = matrix
    session.board = board
    now = datetime.now()

    if resume_kif_path is not None:
        session.moves_usi = list(resume_moves)
        session.game_id = resume_game_id
        new_path = resume_kif_path.with_name(f"{KIF_PREFIX_PLAYING}{resume_game_id}.kif")
        if resume_kif_path != new_path:
            resume_kif_path.rename(new_path)
        session.kif_path = new_path
        # 累計時間は元の対局開始時刻からそのまま積み上げる（中断していた間の実時間も
        # 含む。ユーザー確認済み：複雑な特別扱いより、シンプルで壊れにくい方を優先）。
        # ただし直前の手からの消費時間は、中断中の実時間を「今回の手の思考時間」として
        # 記録してしまうと明らかに不自然なため、再開した今この瞬間を起点にする。
        session.start_time = resume_start_time or now
        session.last_move_time = now
    else:
        session.moves_usi = []
        game_id = now.strftime("%Y%m%d_%H-%M")
        session.game_id = game_id
        session.kif_path = RUNTIME_GAMES_DIR / f"{KIF_PREFIX_PLAYING}{game_id}.kif"
        session.kif_path.write_text(
            f"開始日時：{now.strftime('%Y/%m/%d %H:%M:%S')}\n手数----指手---------消費時間--\n",
            encoding="utf-8",
        )
        session.start_time = now
        session.last_move_time = now

    session.state = GameState.PLAYING
    return {"status": "playing", "game_id": session.game_id}


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

    5回目UAT課題④：`resumable`（終局していない棋譜＝再開対象になるか）を追加。
    6回目UAT課題④：一覧の並び順を、ファイル名の生の文字列ではなく日時の数字部分
    （`_kif_chronological_key`）でソートするよう変更——以前は状態プレフィックスの
    文字コードでグルーピングされ、対局中止/完了が時系列を無視してブロック化していた。
    """
    return {"games": _list_games(date)}


def _list_games(date: str | None) -> list[dict]:
    date_filter = date.replace("-", "") if date else None
    games = []
    for path in sorted(
        RUNTIME_GAMES_DIR.glob("*.kif"),
        key=lambda p: _kif_chronological_key(p.stem),
        reverse=True,
    ):
        m = re.search(r"\d{8}", path.stem)
        date_part = m.group(0) if m else ""
        if date_filter and date_part != date_filter:
            continue
        games.append({
            "id": _strip_kif_prefix(path.stem),
            "filename": path.name,
            "resumable": _is_resumable(path.name),
        })
    return games


@app.get("/games/{game_id}")
async def get_game(game_id: str):
    """3回目UAT課題③：ファイル名の状態プレフィックスが対局終了/中止のタイミングで変わるため、
    idから直接パスを構築できない。プレフィックスを除いた部分が一致するファイルを探す。
    """
    kif = _get_game_kif(game_id)
    if kif is None:
        raise HTTPException(404, "game not found")
    return {"id": game_id, "kif": kif}


def _get_game_kif(game_id: str) -> str | None:
    for path in RUNTIME_GAMES_DIR.glob("*.kif"):
        if _strip_kif_prefix(path.stem) == game_id:
            return path.read_text(encoding="utf-8")
    return None


# ===== Webブラウザ用の棋譜一覧・閲覧画面（追加要望、2026-06-23） =====
# Androidアプリの棋譜一覧（GameListScreen.kt）と同じGET /games・GET /games/{id}を
# そのままブラウザから叩くだけの薄いHTML画面。ユーザー要望通り「対局再開」は持たせない
# （PC等のブラウザから中断局を再開する運用は想定していないため、再開ボタンの表示自体を
# 省略——既存の/calibration/photo等の対局フローには一切手を加えていない）。
# 共有・コピーはAndroidの共有シートに相当する操作がブラウザにはないため、
# navigator.share()（対応ブラウザのみ、OSの共有シートが出る）→未対応ならクリップボード
# コピーにフォールバックする、という2段構成にした。
_WEB_STYLE = """
<style>
  body { font-family: sans-serif; margin: 0; padding: 16px; background: #fafafa; color: #222; }
  h1 { font-size: 1.2rem; }
  .game-row { display: flex; justify-content: space-between; align-items: center;
              padding: 10px 12px; margin-bottom: 6px; background: #fff; border-radius: 6px;
              box-shadow: 0 1px 2px rgba(0,0,0,0.1); text-decoration: none; color: #222; }
  /* 2026-06-23、スマホで一覧の項目が1タップで遷移しない不具合の修正：
     タッチ端末のブラウザは:hoverを「タップ＝即クリック」ではなく「まず仮想ホバー状態に
     入る」と解釈することがあり、その場合1回目のタップではホバー表示が付くだけでリンクへ
     遷移せず、2回目のタップで初めて遷移する（よくあるモバイルブラウザの挙動）。
     hoverを本当にサポートするポインタ環境（マウス等）にだけ適用するよう絞り込み、
     タッチ端末では:hoverスタイル自体を無効にしてこの問題を回避する。 */
  @media (hover: hover) and (pointer: fine) {
    .game-row:hover { background: #f0f0f0; }
  }
  input[type=date] { padding: 6px; margin-bottom: 12px; }
  button { padding: 8px 14px; margin-right: 8px; margin-top: 8px; cursor: pointer; }
  pre { background: #fff; padding: 12px; border-radius: 6px; white-space: pre-wrap;
        word-break: break-all; box-shadow: 0 1px 2px rgba(0,0,0,0.1); }
  .back-link { display: inline-block; margin-bottom: 12px; }
  .empty { color: #888; padding: 12px; }
</style>
"""

# 2026-06-23、7回目UAT課題⑨：一覧の項目・「一覧に戻る」リンクが1回タップしただけでは
# 遷移せず、2回目のタップで初めて遷移する不具合。:hover起因の説（既に修正済み）では
# 説明できず（hoverスタイルを持たないback-linkでも同じ症状）、PCでは再現しない。
# 対策1回目（document.write方式）・対策2回目（HEAD probe＋リトライ方式）は撤回済み
# （詳細はgit履歴参照）。
#
# 2026-06-23、8回目テスト課題⑨⑪：HEAD 405バグ修正後も症状が続いたため、画面上に
# 診断ログを表示してタップ後の挙動を直接観察したところ、決定的な証拠が得られた：
# probeのfetch自体は23〜46msで毎回成功し、その直後に`location.href = url`も
# 実際に呼ばれている（ログに記録される）にもかかわらず、3秒後も同じページに
# 留まり続けていた——つまりこれはネットワーク（LANの詰まり）の問題ではなく、
# **非同期コールバック（Promiseの.then）内から行うlocation.href代入が、このスマホの
# ブラウザでは実質的に無視されている**ことが直接の原因だった。クリックを横取りして
# fetchを挟む方式そのものが、素の同期的なリンククリック（ブラウザネイティブの遷移）が
# 持つはずの効果を失わせていたと考えられる。
# 対策（2026-06-23、3回目・現行）：JSによるクリック横取り・fetch probe・location.href
# 呼び出しを全廃し、`<a href>`のみによるブラウザ標準の遷移に戻した。HEAD probeで
# 確認した通りサーバー応答自体は速い（数十ms）ため、素のクリックで詰まる理由はない。
# 2026-06-23、8回目テスト課題⑨続報：JS全廃＋Cache-Control修正後、ユーザーから新たな
# 決定的な観察が得られた——「1回目のタップは絶対に遷移しない。2回目は1回目と同じボタンで
# なくてもよい（別のリンクでもよい）」。サーバーログでも、1回目のタップに対応するHTTP
# リクエストは一度も記録されず、2回目のタップで初めて1件のGETが記録される、という
# パターンが何度も繰り返し確認された。クリックを横取りするJSは存在しない（素の
# `<a href>`のみ）ため、「クリックは発生したが遷移処理でつまずく」という状態はもはや
# 存在しえない——1回目のタップはDOMのクリックイベントとして一切発生していないと判断できる。
# これは「ページ読み込み直後の最初のタップは、ブラウザ（アドレスバー縮小・タブの
# フォーカス確立等）に消費され、ページ内のコンテンツには届かない」という、Android系
# ブラウザでよく報告される現象と一致する（タップ対象に依存しないのはこの説明と整合する：
# どのリンクをタップしても、最初の1回はページではなくブラウザ自身への入力として処理される）。
# 対策：ページ読み込み完了時に`window.scrollTo`/`window.focus`を呼び、ブラウザの
# チューム（アドレスバー等）の状態を読み込み時点で確定させておく、よく知られた回避策を
# 試す。クリックの横取り・fetch・location.href操作は一切行わない（前回の教訓を踏まえ、
# 素のリンク遷移そのものには触れない）。
_WEB_NAV_SCRIPT = """
<script>
window.addEventListener('load', function () {
  window.scrollTo(0, 1);
  window.focus();
});
</script>
"""


@app.get("/web")
async def web_root():
    return RedirectResponse(url="/web/games")


_NO_STORE_HEADERS = {"Cache-Control": "no-store, no-cache, must-revalidate"}


@app.api_route("/web/games", methods=["GET", "HEAD"], response_class=HTMLResponse)
async def web_games_list(date: str | None = None):
    """2026-06-23、実機検証で発覚：一覧ページ自体（このエンドポイント）はスマホからでも
    届くが、ページ内のJSが続けて発行する2回目の通信（fetch('/games')）がスマホからだと
    届かない／止まることがあった（PCの同じブラウザでは問題なし）。Androidネイティブの
    OkHttpで以前経験した「1回目の直後の2回目のリクエストが届かない」事象
    （RetrofitClient.kt参照）と同じ系統の、このLAN特有の問題と疑われる。原因の真因は
    特定できていないが、回避策として一覧データをこのページ自体に直接埋め込み、JSからの
    追加fetchを行わない構成に変更した（日付フィルタも素のページ遷移にした）。

    2026-06-23、8回目テスト課題⑨：JS制御を撤廃した版をデプロイしてもスマホで症状が
    再現し続けたが、ユーザーのスクリーンショットを見ると画面下部に**削除したはずの
    診断ログJSがまだ表示されていた**——つまりブラウザがこのページをキャッシュしており、
    新しいサーバーコード（JS削除版）ではなく古いHTMLをそのまま再表示していたことが
    判明した。`Cache-Control`を一切返していなかったため、ブラウザの既定のヒューリスティック
    キャッシュ（特に戻る操作やタブ復帰時）に乗ってしまっていたと考えられる。
    `no-store`を明示し、毎回サーバーから最新のHTMLを取得させるようにした。
    """
    games = _list_games(date)
    if games:
        rows = "\n".join(
            f'<a class="game-row" href="/web/games/{html_escape(g["id"])}">{html_escape(g["filename"])}</a>'
            for g in games
        )
        list_html = f'<div id="list">{rows}</div>'
    else:
        list_html = '<div id="list" class="empty">対局が見つかりません</div>'
    date_attr = f' value="{html_escape(date)}"' if date else ""
    html = f"""<!DOCTYPE html>
<html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>棋譜一覧</title>{_WEB_STYLE}</head>
<body>
<h1>棋譜一覧</h1>
<input type="date" id="dateFilter"{date_attr}
       onchange="location.href = '/web/games' + (this.value ? '?date=' + this.value : '')">
{list_html}
{_WEB_NAV_SCRIPT}
</body></html>"""
    return HTMLResponse(content=html, headers=_NO_STORE_HEADERS)


@app.api_route("/web/games/{game_id}", methods=["GET", "HEAD"], response_class=HTMLResponse)
async def web_game_detail(game_id: str):
    """web_games_listと同じ理由で、KIF本文もページ内JSのfetchに頼らずこのページ自体に
    直接埋め込む構成にした（2026-06-23）。

    2026-06-23、8回目テスト課題⑪：このルートと web_games_list が@app.get（GETのみ登録）
    だったため、一覧クリック時にブラウザが送るHEAD probe（_WEB_NAV_SCRIPT参照）が
    毎回405 Method Not Allowedで弾かれていた。実機ログで確認済み。これが「2回タップ
    しないと遷移しない」（課題⑨）の真因だった可能性が高い——probeが毎回確実に失敗する
    ので、毎回リトライの末に約2秒待ってからフォールバック遷移する動きになっていたはず。
    methods=["GET", "HEAD"]を明示してHEADも受け付けるようにした。

    2026-06-23、8回目テスト課題⑨：`Cache-Control`を返していなかったため、ブラウザが
    このページをキャッシュし、サーバー側を新しいコードに更新してもスマホでは古いHTML
    （削除したはずのJSを含む）が再表示され続ける事象が発覚（`web_games_list`の同日付の
    docstring参照）。`no-store`を明示した。"""
    kif = _get_game_kif(game_id)
    if kif is None:
        body = "<h1>対局が見つかりません</h1>"
        kif_js_literal = "null"
    else:
        body = f'<h1>{html_escape(game_id)}</h1>\n<pre id="kif">{html_escape(kif)}</pre>'
        # </script>でHTMLが途中で切れないよう、念のため"</"をエスケープしてから埋め込む
        kif_js_literal = json.dumps(kif).replace("</", "<\\/")
    game_id_js_literal = json.dumps(game_id).replace("</", "<\\/")
    html = f"""<!DOCTYPE html>
<html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>棋譜詳細</title>{_WEB_STYLE}</head>
<body>
<a class="back-link" href="/web/games">← 一覧に戻る</a>
{body}
<div>
  <button id="shareBtn">共有</button>
  <button id="copyBtn">コピー（分析Tool貼付用）</button>
</div>
<div id="msg"></div>
<script>
const gameId = {game_id_js_literal};
const kifText = {kif_js_literal};
// このサーバーはLAN内のhttp（暗号化なし）でアクセスする運用のため、navigator.clipboardは
// 「セキュアコンテキスト」（https or localhost）でないブラウザでは使えない可能性がある。
// 使えない場合は非表示textarea+document.execCommand('copy')（非推奨だが平文httpでも動く）に
// フォールバックする。
async function copyText(text) {{
  if (navigator.clipboard && window.isSecureContext) {{
    await navigator.clipboard.writeText(text);
    return;
  }}
  const ta = document.createElement('textarea');
  ta.value = text;
  ta.style.position = 'fixed';
  ta.style.opacity = '0';
  document.body.appendChild(ta);
  ta.select();
  document.execCommand('copy');
  document.body.removeChild(ta);
}}
document.getElementById('shareBtn').addEventListener('click', async () => {{
  if (navigator.share) {{
    try {{ await navigator.share({{ title: gameId, text: kifText }}); return; }} catch (e) {{ /* キャンセル等は無視 */ }}
  }}
  await copyText(kifText);
  document.getElementById('msg').textContent = 'お使いのブラウザは共有に対応していないため、クリップボードにコピーしました';
}});
document.getElementById('copyBtn').addEventListener('click', async () => {{
  await copyText(kifText);
  document.getElementById('msg').textContent = 'コピーしました';
}});
</script>
{_WEB_NAV_SCRIPT}
</body></html>"""
    return HTMLResponse(content=html, headers=_NO_STORE_HEADERS)
