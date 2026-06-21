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

from itertools import combinations
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


# ===== 赤丸キャリブレーション（自動検出。前PJ run_realtime.py から移植） =====
# 2026-06-21、実機テストで「4隅の手動タップが難しすぎる」というユーザー指摘を受け、
# 「まず赤丸で自動キャリブレーション、検出できなければ人間が4隅タップ」という前PJと同じ
# 二段構成に変更。物理的な盤に前PJ運用時と同じ赤丸マーカーがある前提。
RED_LOWER1 = np.array([0, 120, 80])
RED_UPPER1 = np.array([10, 255, 255])
RED_LOWER2 = np.array([165, 120, 80])
RED_UPPER2 = np.array([180, 255, 255])
CIRC_MIN = 0.55
BOARD_RADIUS_RATIO = 0.006


def detect_red_circles(img):
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    mask = cv2.bitwise_or(cv2.inRange(hsv, RED_LOWER1, RED_UPPER1),
                           cv2.inRange(hsv, RED_LOWER2, RED_UPPER2))
    k = np.ones((5, 5), np.uint8)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, k)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, k)
    cnts, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    circles = []
    for c in cnts:
        a = cv2.contourArea(c)
        if a < 200:
            continue
        peri = cv2.arcLength(c, True)
        if peri == 0:
            continue
        circ = 4 * np.pi * a / (peri * peri)
        (x, y), r = cv2.minEnclosingCircle(c)
        if 8 <= r <= 80:
            circles.append((float(x), float(y), float(r), float(circ)))
    return circles


def _convex_quad_area(pts):
    """4点の凸包の面積。3点が一直線上等の縮退ケースは自然に小さい面積になる。"""
    hull = cv2.convexHull(np.array(pts, dtype=np.float32))
    return cv2.contourArea(hull)


def select_board_corners(circles, shape):
    """検出された赤丸候補から、盤の4隅とみなす4点を選ぶ。

    2026-06-21、方針変更（前PJの「画像4隅それぞれに最も近い赤丸を選ぶ」方式から変更——
    ユーザー承認済み、このプロジェクト独自に作り直す対象として赤丸検出自体ではなく
    この角選択ロジックのみが対象）。旧方式は画面の隅近くに背景の赤い物体（小物・ボール等）
    が入ると、それを盤の角と誤選択する弱点があった。

    新方式：候補点から4点を選ぶ全組み合わせのうち、凸包の面積が最大になる4点を盤の角とする。
    盤の四隅は候補の中で最も広い範囲に張る4点になるはずなので、画面の隅に偶然写り込んだ
    背景の赤い物体（盤の内側寄りにあることが多い）は面積で見れば不利になり選ばれにくい。
    """
    h, w = shape[:2]
    diag = (w ** 2 + h ** 2) ** 0.5
    filt = [c for c in circles if c[3] >= CIRC_MIN]
    if len(filt) < 4:
        filt = [c for c in circles if c[3] >= 0.45]
    if len(filt) < 4:
        return None
    rth = diag * BOARD_RADIUS_RATIO
    cand = [c for c in filt if c[2] >= rth]
    if len(cand) < 4:
        cand = sorted(filt, key=lambda c: -c[2])[:max(4, len(cand))]
    if len(cand) < 4:
        return None

    points = [(c[0], c[1]) for c in cand]
    best_area = -1.0
    best_combo = None
    for combo in combinations(points, 4):
        area = _convex_quad_area(combo)
        if area > best_area:
            best_area = area
            best_combo = combo
    if best_combo is None:
        return None
    return list(best_combo)


def calibrate_from_image(img):
    """画像から赤丸4個を自動検出してキャリブレーション行列を計算する。
    検出できなければNoneを返す（呼び出し側は人間の4隅タップにフォールバックする）。
    """
    circles = detect_red_circles(img)
    if len(circles) < 4:
        return None
    corners = select_board_corners(circles, img.shape)
    if corners is None:
        return None
    return compute_calib(order_points(corners))


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


def compare_to_initial(recognized):
    """recognizedを将棋の初期配置と比較する。

    キャリブレーションは常に対局開始前の初期配置に対して行うため、認識結果が初期配置と
    一致するかどうかは人間が9x9を目視確認しなくてもプログラム側で判定できる
    （2026-06-21、実機テストでのユーザー指摘により、キャリブレーション確認UIを
    「人間が見て判断」から「プログラムが自動判定」に変更）。前PJ run_realtime.py の
    INITIAL_BOARD_STD直書きではなく、shogi.Board()の初期状態をboard_to_label_gridに
    通して使う（指し手判定ロジックの初期状態と単一の真実を共有するため）。

    Returns: [(row, col, expected, got), ...]  空リストなら完全一致。
    """
    expected_grid = board_to_label_grid(shogi.Board())
    mismatches = []
    for r in range(9):
        for c in range(9):
            if recognized[r][c] != expected_grid[r][c]:
                mismatches.append((r, c, expected_grid[r][c], recognized[r][c]))
    return mismatches


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
