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
from pathlib import Path

import cv2
import numpy as np
import shogi
import shogi.KIF
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

    overlay_b64 = base64.b64encode(recognition.grid_overlay_jpeg_bytes(img, matrix)).decode("ascii")
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
