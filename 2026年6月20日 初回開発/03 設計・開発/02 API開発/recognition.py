"""
盤面認識・指し手判定ロジック。

前PJ（\\YukiYoshiNAS\\Shogiban-kaiseki-tool\\src\\run_realtime.py）から、本プロジェクトで
必要な部分（モデル推論・4隅キャリブレーション・指し手判定・KIF生成）を複製・移植したもの。
前PJは完了・凍結済みのため直接編集せず、本ファイルを複製先として独立に保守する。

run_realtime.pyとの差分:
- 赤丸自動検出・青三角向き判定・手動キャリブレーションUIは移植していない
  （本プロジェクトはネイティブカメラで向きが構造的に固定され、4隅は人間が直接タップするため不要）。
- classify_frameの採用判定を変更：MOVE_DIFF_THRESHOLD<=2の曖昧な許容ではなく、
  diffが完全に0になる候補のみ採用する（アーキテクチャ検討.md 2節で確定済みの修正）。
  Bluetoothボタン1クリック1枚撮影という運用上、二重発火・撮り忘れで誤った手を
  静かに採用するリスクが前PJより高いため。
"""

from pathlib import Path

import cv2
import numpy as np
import shogi
import torch
import torch.nn as nn
from PIL import Image
from torchvision import models, transforms

ALL_LABELS = [
    "empty",
    "sente_fu", "sente_kyo", "sente_kei", "sente_gin", "sente_kin",
    "sente_kaku", "sente_hi", "sente_ou",
    "sente_tokin", "sente_nari_kyo", "sente_nari_kei", "sente_nari_gin",
    "sente_uma", "sente_ryu",
    "gote_fu", "gote_kyo", "gote_kei", "gote_gin", "gote_kin",
    "gote_kaku", "gote_hi", "gote_ou",
    "gote_tokin", "gote_nari_kyo", "gote_nari_kei", "gote_nari_gin",
    "gote_uma", "gote_ryu",
]
NUM_CLASSES = len(ALL_LABELS)

LABEL_TO_PIECE = {
    "fu": shogi.PAWN, "kyo": shogi.LANCE, "kei": shogi.KNIGHT, "gin": shogi.SILVER,
    "kin": shogi.GOLD, "kaku": shogi.BISHOP, "hi": shogi.ROOK, "ou": shogi.KING,
    "tokin": shogi.PROM_PAWN, "nari_kyo": shogi.PROM_LANCE, "nari_kei": shogi.PROM_KNIGHT,
    "nari_gin": shogi.PROM_SILVER, "uma": shogi.PROM_BISHOP, "ryu": shogi.PROM_ROOK,
}
PIECE_TO_LABEL = {v: k for k, v in LABEL_TO_PIECE.items()}

GRID_SIZE = 9
CELL_PX = 100
WARP_SIDE = CELL_PX * GRID_SIZE  # 900


# ===== モデル =====
def load_model(model_folder):
    model_path = Path(model_folder) / "best_model.pth"
    model = models.mobilenet_v2(weights=None)
    in_f = model.classifier[1].in_features
    model.classifier = nn.Sequential(
        nn.Dropout(0.3), nn.Linear(in_f, 256), nn.ReLU(),
        nn.Dropout(0.2), nn.Linear(256, NUM_CLASSES))
    model.load_state_dict(torch.load(model_path, map_location="cpu"))
    model.eval()
    return model


_transform = transforms.Compose([
    transforms.Resize((224, 224)), transforms.ToTensor(),
    transforms.Normalize([0.485, 0.456, 0.406], [0.229, 0.224, 0.225])])


def predict_board(model, warped, cell_px=CELL_PX, grid_size=GRID_SIZE):
    labels = [[None] * grid_size for _ in range(grid_size)]
    for row in range(grid_size):
        for col in range(grid_size):
            x1, y1 = col * cell_px, row * cell_px
            cell = warped[y1:y1 + cell_px, x1:x1 + cell_px]
            pil = Image.fromarray(cv2.cvtColor(cell, cv2.COLOR_BGR2RGB))
            t = _transform(pil).unsqueeze(0)
            with torch.no_grad():
                out = model(t)
                pred = out.max(1)[1]
            labels[row][col] = ALL_LABELS[pred.item()]
    return labels


# ===== キャリブレーション（盤の四隅4点→透視変換） =====
def order_points(pts):
    pts = np.array(pts, dtype="float32")
    s = pts.sum(1)
    d = np.diff(pts, axis=1)
    o = np.zeros((4, 2), dtype="float32")
    o[0] = pts[np.argmin(s)]
    o[1] = pts[np.argmin(d)]
    o[2] = pts[np.argmax(s)]
    o[3] = pts[np.argmax(d)]
    return o


def compute_calib(corners, grid_size=GRID_SIZE, cell_px=CELL_PX):
    side = cell_px * grid_size
    dst = np.array([[0, 0], [side, 0], [side, side], [0, side]], dtype="float32")
    return cv2.getPerspectiveTransform(corners, dst)


def warp_board(img, M):
    return cv2.warpPerspective(img, M, (WARP_SIDE, WARP_SIDE))


def save_grid_overlay(img, M, out_path, grid_size=GRID_SIZE, cell_px=CELL_PX):
    """キャリブレーション結果にグリッドを重ねた画像を保存する（人間の目視確認用）"""
    warped = warp_board(img, M)
    vis = warped.copy()
    side = cell_px * grid_size
    for i in range(grid_size + 1):
        cv2.line(vis, (0, i * cell_px), (side, i * cell_px), (0, 200, 0), 2)
        cv2.line(vis, (i * cell_px, 0), (i * cell_px, side), (0, 200, 0), 2)
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    cv2.imencode(".png", vis)[1].tofile(str(out_path))


# ===== 座標・差分・指し手判定 =====
def square_to_rc(sq):
    col = 8 - (sq // 9)
    row = sq % 9
    return row, col


def board_to_label_grid(board):
    """shogi.Boardを9x9ラベルグリッドに変換する"""
    grid = [["empty"] * 9 for _ in range(9)]
    for sq in shogi.SQUARES:
        piece = board.piece_at(sq)
        if piece is None:
            continue
        row, col = square_to_rc(sq)
        piece_name = PIECE_TO_LABEL.get(piece.piece_type)
        if piece_name is None:
            continue
        color_prefix = "sente" if piece.color == shogi.BLACK else "gote"
        grid[row][col] = f"{color_prefix}_{piece_name}"
    return grid


def diff_count(grid_a, grid_b):
    n = 0
    for r in range(9):
        for c in range(9):
            if grid_a[r][c] != grid_b[r][c]:
                n += 1
    return n


def _best_legal_sequences(board, recognized, depth):
    """boardからdepth手先までの全合法手列を試し、recognizedとの差分が最小の手列を返す。
    Returns: (best_diff, [手列(shogi.Moveのリスト), ...])
    """
    if depth == 1:
        best_diff = None
        best_seqs = []
        for m1 in board.legal_moves:
            b1 = shogi.Board()
            b1.set_sfen(board.sfen())
            b1.push(m1)
            d = diff_count(board_to_label_grid(b1), recognized)
            if best_diff is None or d < best_diff:
                best_diff, best_seqs = d, [[m1]]
            elif d == best_diff:
                best_seqs.append([m1])
        return best_diff, best_seqs

    best_diff = None
    best_seqs = []
    for m1 in board.legal_moves:
        b1 = shogi.Board()
        b1.set_sfen(board.sfen())
        b1.push(m1)
        for m2 in b1.legal_moves:
            b2 = shogi.Board()
            b2.set_sfen(b1.sfen())
            b2.push(m2)
            d = diff_count(board_to_label_grid(b2), recognized)
            if best_diff is None or d < best_diff:
                best_diff, best_seqs = d, [[m1, m2]]
            elif d == best_diff:
                best_seqs.append([m1, m2])
    return best_diff, best_seqs


def classify_frame(board, recognized):
    """
    直前の写真ではなく、現在確定しているboardと今回の認識結果を比較し、それを説明できる
    合法手を合法手全体から総当たりで探す。

    前PJと異なり、diffが完全に0になる候補のみ採用する（厳格化）。前PJの
    MOVE_DIFF_THRESHOLD<=2方式は「ほぼ説明できる」候補も採用してしまい、本来あるべき
    手を静かに取りこぼして局面追跡がズレる既知の弱点があった（CLAUDE.md記載）。
    本プロジェクトはBluetoothシャッター1クリック1枚撮影で、二重発火・撮り忘れにより
    「本来あるはずの中間局面が無い」フレームが届く可能性があるため、曖昧な許容はせず
    diff0===0の完全一致のみ採用し、それ以外は depth2 へ、最終的にダメなら "error" とする。

    Returns: (status, payload)
      status="move":     payload=採用したUSI文字列のリスト（1〜2手）
      status="nochange": payload=0（写真がboardと完全一致、駒のずれ等のノイズと判断）
      status="error":    payload=デバッグ情報dict
    """
    diff0 = diff_count(board_to_label_grid(board), recognized)
    if diff0 == 0:
        return "nochange", diff0

    for depth in (1, 2):
        best_diff, best_seqs = _best_legal_sequences(board, recognized, depth)
        if best_diff == 0:
            if len(best_seqs) == 1:
                return "move", [m.usi() for m in best_seqs[0]]
            return "error", {
                "reason": "ambiguous", "depth": depth, "diff0": diff0,
                "best_diff": best_diff,
                "candidates": [[m.usi() for m in seq] for seq in best_seqs],
            }

    return "error", {"reason": "no_match", "diff0": diff0}


# ===== KIF出力 =====
FILE_JA = ['', '１', '２', '３', '４', '５', '６', '７', '８', '９']
RANK_JA = ['', '一', '二', '三', '四', '五', '六', '七', '八', '九']
PIECE_JA = {'p': '歩', 'l': '香', 'n': '桂', 's': '銀', 'g': '金', 'b': '角', 'r': '飛', 'k': '玉',
            '+p': 'と', '+l': '成香', '+n': '成桂', '+s': '成銀', '+b': '馬', '+r': '龍'}
_FILE_MAP = {'a': 1, 'b': 2, 'c': 3, 'd': 4, 'e': 5, 'f': 6, 'g': 7, 'h': 8, 'i': 9}


def _square_ja(sqn):
    return f"{FILE_JA[int(sqn[0])]}{RANK_JA[_FILE_MAP[sqn[1]]]}"


def move_to_text(board, move):
    """boardはmoveを指す前の局面。(kif_text, speech_text)を返す。
    kif_textはKIF用（移動元の半角数字つき）、speech_textはTTS読み上げ用（移動元なし）。
    """
    dst_ja = _square_ja(shogi.SQUARE_NAMES[move.to_square])
    if move.drop_piece_type:
        pj = PIECE_JA.get(shogi.PIECE_SYMBOLS[move.drop_piece_type], '?')
        text = f"{dst_ja}{pj}打"
        return text, text

    src = shogi.SQUARE_NAMES[move.from_square]
    pc = board.piece_at(move.from_square)
    pj = PIECE_JA.get(shogi.PIECE_SYMBOLS[pc.piece_type] if pc else '?', '?')
    pr = "成" if move.promotion else ""
    speech_text = f"{dst_ja}{pj}{pr}"
    kif_text = f"{speech_text}({src[0]}{_FILE_MAP[src[1]]})"
    return kif_text, speech_text


def save_kif(moves_usi, out_path):
    """moves_usi全体から完全なKIFファイルを書き出す（終局時の確定保存用）"""
    lines = ["手数----指手---------消費時間--"]
    board = shogi.Board()
    for i, usi in enumerate(moves_usi):
        try:
            b = shogi.Board()
            b.set_sfen(board.sfen())
            b.push_usi(usi)
            move = list(b.move_stack)[-1]
            kif_text, _ = move_to_text(board, move)
            lines.append(f"{i + 1:4d} {kif_text} (00:00/00:00:00)")
            board.push(move)
        except Exception as e:
            lines.append(f"{i + 1:4d} (ERROR: {e})")
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
