"""APIサーバ本体。

`/health` と `/photo`（写真を受け取って保存するだけの通信確認用ダミー）は初期動作確認で使った
ものをそのまま残してある。本番の対局フローは `/calibration/photo` 以降の新エンドポイント。

エンドポイント構成はアーキテクチャ検討.md 3節の案に対応:
  POST /calibration/photo   キャリブレーション写真+盤の4隅座標 → 9x9認識結果（人間確認用）
  POST /calibration/confirm 確認OK → 対局開始（盤=将棋の初期配置、KIF新規作成）
  POST /move                1手分の写真 → classify_frameで判定 → KIF追記+読み上げテキスト
  POST /game/end             終局通知 → KIF確定、状態をidleに戻す
  GET  /games                KIF一覧（開始日付で絞り込み可）
  GET  /games/{game_id}      指定KIFの内容取得

盤面認識・指し手判定ロジックは recognition.py、対局状態は session.py のシングルトンに分離。
"""

import json
import time
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np
import shogi
from fastapi import FastAPI, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse

import recognition
from session import GameState, PendingCalibration, session

app = FastAPI()

BASE_DIR = Path(__file__).parent
MODEL_DIR = BASE_DIR / "models"
RUNTIME_DIR = BASE_DIR / "runtime"
RUNTIME_PHOTOS_DIR = RUNTIME_DIR / "photos"
RUNTIME_GAMES_DIR = RUNTIME_DIR / "games"
RUNTIME_PHOTOS_DIR.mkdir(parents=True, exist_ok=True)
RUNTIME_GAMES_DIR.mkdir(parents=True, exist_ok=True)

# 旧来の通信確認用ダミーエンドポイント（/photo）が使っていた保存先。本番フローは runtime/ 配下を使う。
UPLOAD_DIR = BASE_DIR / "received_photos"
UPLOAD_DIR.mkdir(exist_ok=True)

print("モデル読み込み中...")
MODEL = recognition.load_model(MODEL_DIR)
print("モデル読み込み完了。")


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
    """写真を受け取りキャリブレーションする。idle・ready のどちらからでも呼べる
    （readyからはキャリブレーションのやり直しを意味する）。

    pointsを省略すると、まず前PJと同じ赤丸4個の自動検出を試す
    （2026-06-21、「4隅の手動タップが難しすぎる」というユーザー指摘により、
    手動タップが先ではなく自動検出が先になる二段構成に変更）。
    自動検出に失敗した場合は status="calibration_failed" を返すので、アプリ側は
    同じ写真に対して盤の4隅座標を添えてこのエンドポイントを再度呼ぶ（人間のフォールバック）。
    pointsを指定した場合は常にその4点座標（JSON配列 "[[x1,y1],...,[x4,y4]]"、順不同）を使う。
    """
    # 2026-06-21、実機で「サーバはuvicornログ上200 OKで完了しているのにクライアント側が
    # タイムアウトする」という再現性のある事象を調査するため、サーバ内処理の各段階の
    # 所要時間を計測してコンソールに出す（原因調査用、恒久的なログではない）。
    t_start = time.perf_counter()
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

    if session.state not in (GameState.IDLE, GameState.READY):
        raise HTTPException(409, f"calibration not allowed in state={session.state.value}")

    if points is None:
        matrix = recognition.calibrate_from_image(img)
        t_calib = time.perf_counter()
        if matrix is None:
            print(f"[timing] body_read={t_body_read-t_start:.2f}s decode={t_decode-t_body_read:.2f}s "
                  f"save={t_save-t_decode:.2f}s calib={t_calib-t_save:.2f}s -> calibration_failed", flush=True)
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

    session.pending_calibration = PendingCalibration(matrix=matrix, recognized=recognized)
    session.state = GameState.READY
    t_end = time.perf_counter()
    print(f"[timing] body_read={t_body_read-t_start:.2f}s decode={t_decode-t_body_read:.2f}s "
          f"save={t_save-t_decode:.2f}s calib={t_calib-t_save:.2f}s predict={t_predict-t_calib:.2f}s "
          f"total={t_end-t_start:.2f}s", flush=True)
    return {
        "status": "ready",
        "recognized": recognized,
        "matches_initial": len(mismatches) == 0,
        "mismatch_count": len(mismatches),
    }


@app.post("/calibration/confirm")
async def calibration_confirm():
    """人間が認識結果を確認OKした際に呼ぶ。対局開始（盤=将棋の初期配置）。"""
    if session.state != GameState.READY or session.pending_calibration is None:
        raise HTTPException(409, "no pending calibration to confirm")

    session.calib_matrix = session.pending_calibration.matrix
    session.pending_calibration = None
    session.board = shogi.Board()
    session.moves_usi = []
    session.game_id = f"game_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    session.kif_path = RUNTIME_GAMES_DIR / f"{session.game_id}.kif"
    session.kif_path.write_text("手数----指手---------消費時間--\n", encoding="utf-8")
    session.state = GameState.PLAYING
    return {"status": "playing", "game_id": session.game_id}


@app.post("/move")
async def post_move(file: UploadFile):
    """1手分の写真を受け取り、classify_frameで指し手を判定する。
    成功時はKIFに1行追記し、読み上げ用テキストを返す。失敗時はリトライ判断をアプリ側に委ねる
    （アプリ要件: エラー時は自動で1回だけ撮り直し、それでも失敗ならエラー音で諦める）。
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
        for usi in payload:
            move = shogi.Move.from_usi(usi)
            kif_text, speech_text = recognition.move_to_text(session.board, move)
            session.board.push(move)
            session.moves_usi.append(usi)
            move_number = len(session.moves_usi)
            with open(session.kif_path, "a", encoding="utf-8") as f:
                f.write(f"{move_number:4d} {kif_text} (00:00/00:00:00)\n")
            speech_texts.append(speech_text)
        return {
            "status": "move",
            "moves": payload,
            "speech_text": "、".join(speech_texts),
            "move_count": len(session.moves_usi),
        }

    if status == "nochange":
        return {"status": "nochange"}

    return JSONResponse(status_code=200, content={"status": "error", "detail": payload})


@app.post("/game/end")
async def game_end():
    """終局通知。KIFは/moveの時点で逐次追記済みのため、ここでは状態をidleに戻すのみ。"""
    if session.state != GameState.PLAYING:
        raise HTTPException(409, f"game/end not allowed in state={session.state.value}")

    session.state = GameState.FINISHING
    result = {
        "status": "idle",
        "game_id": session.game_id,
        "move_count": len(session.moves_usi),
    }
    session.reset()
    return result


@app.get("/games")
async def list_games(date: str | None = None):
    """KIF一覧。dateを指定すると対局開始日付（YYYYMMDD or YYYY-MM-DD）で絞り込む。"""
    date_filter = date.replace("-", "") if date else None
    games = []
    for path in sorted(RUNTIME_GAMES_DIR.glob("game_*.kif"), reverse=True):
        # game_id形式: game_YYYYMMDD_HHMMSS
        date_part = path.stem.split("_")[1] if len(path.stem.split("_")) > 1 else ""
        if date_filter and date_part != date_filter:
            continue
        games.append({"id": path.stem, "filename": path.name})
    return {"games": games}


@app.get("/games/{game_id}")
async def get_game(game_id: str):
    path = RUNTIME_GAMES_DIR / f"{game_id}.kif"
    if not path.exists():
        raise HTTPException(404, "game not found")
    return {"id": game_id, "kif": path.read_text(encoding="utf-8")}
