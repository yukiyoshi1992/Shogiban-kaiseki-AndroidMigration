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
    # 2026-06-22、UATで処理が遅いと指摘されたため高速化：前PJ由来の実装は81マスを
    # 1枚ずつモデルに通していた（推論のたびにPython/PyTorchの呼び出しオーバーヘッドが
    # 81回発生）。eval()モード（load_model内で設定済み）ではBatchNormが学習時の
    # 移動平均統計を使い、Dropoutも無効化されるため、バッチサイズを変えても各マスの
    # 推論結果はマス単位ですでに独立しており、1枚ずつ回しても81枚まとめて回しても
    # 数値的に同じ結果になる。81マスを1回のバッチにまとめて1回のフォワードパスで
    # 推論するよう変更（結果は変えず、実行方法だけ変更）。
    tensors = []
    for row in range(grid_size):
        for col in range(grid_size):
            x1, y1 = col * cell_px, row * cell_px
            cell = warped[y1:y1 + cell_px, x1:x1 + cell_px]
            pil = Image.fromarray(cv2.cvtColor(cell, cv2.COLOR_BGR2RGB))
            tensors.append(_transform(pil))
    batch = torch.stack(tensors)
    with torch.no_grad():
        out = model(batch)
        preds = out.max(1)[1]

    labels = [[None] * grid_size for _ in range(grid_size)]
    for idx in range(grid_size * grid_size):
        row, col = divmod(idx, grid_size)
        labels[row][col] = ALL_LABELS[preds[idx].item()]
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


def grid_overlay_jpeg_bytes(img, M, grid_size=GRID_SIZE, cell_px=CELL_PX):
    """save_grid_overlayと同じ緑グリッド重畫画像をファイルに保存せずJPEGバイト列として返す。
    2026-06-22、UAT課題②：手動タップ後に「マスが正しく取れているか」を人間が目視確認する
    画面用（前PJ同様の緑線オーバーレイ）。APIレスポンスに直接載せて返すための変種。
    PNGだと約1.5MBになりレスポンスが重いため、JPEG（quality=85）で約7分の1に圧縮する
    （緑線は太く塗っているため、JPEG圧縮による劣化があっても目視確認の用途には十分）。
    """
    warped = warp_board(img, M)
    vis = warped.copy()
    side = cell_px * grid_size
    for i in range(grid_size + 1):
        cv2.line(vis, (0, i * cell_px), (side, i * cell_px), (0, 200, 0), 2)
        cv2.line(vis, (i * cell_px, 0), (i * cell_px, side), (0, 200, 0), 2)
    return cv2.imencode(".jpg", vis, [cv2.IMWRITE_JPEG_QUALITY, 85])[1].tobytes()


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


def select_board_corners(circles, shape, img=None):
    """検出された赤丸候補から、盤の4隅とみなす4点を選ぶ。

    2026-06-21、方針変更（前PJの「画像4隅それぞれに最も近い赤丸を選ぶ」方式から変更——
    ユーザー承認済み、このプロジェクト独自に作り直す対象として赤丸検出自体ではなく
    この角選択ロジックのみが対象）。旧方式は画面の隅近くに背景の赤い物体（小物・ボール等）
    が入ると、それを盤の角と誤選択する弱点があった。

    新方式：候補点から4点を選ぶ全組み合わせのうち、凸包の面積が最大になる4点を盤の角とする。
    盤の四隅は候補の中で最も広い範囲に張る4点になるはずなので、画面の隅に偶然写り込んだ
    背景の赤い物体（盤の内側寄りにあることが多い）は面積で見れば不利になり選ばれにくい。

    2026-06-22、UAT課題①：強い候補（円形度・半径とも基準を満たす）が3点しか見つからない場合の
    救済を追加（imgを渡した場合のみ有効）。実際の失敗写真2枚を数値で解析したところ、4点中3点は
    通常通り検出できるのに残り1点だけ極端に小さく断片化していた——原因はユーザー確認済みで
    「暗所で読めない」のではなく、その箇所の光の反射が強く、赤丸が白飛びして彩度が落ちて
    薄く写っていたこと（実測でもその領域のV（明度）は非常に高く、S（彩度）だけが低い、
    白飛びに典型的な値だった）。色のHSVしきい値自体は他の3点と同じ値で一部のピクセルは通って
    いるが、彩度が低い分マスクが薄くまばらになり、モルフォロジー処理や面積下限フィルタを
    生き残れるだけの大きさが無かった。詳細は`_predict_missing_corner`/`_find_weak_marker_near`参照。
    """
    h, w = shape[:2]
    diag = (w ** 2 + h ** 2) ** 0.5
    filt = [c for c in circles if c[3] >= CIRC_MIN]
    if len(filt) < 4:
        filt = [c for c in circles if c[3] >= 0.45]
    rth = diag * BOARD_RADIUS_RATIO
    cand = [c for c in filt if c[2] >= rth]
    if len(cand) < 4:
        cand = sorted(filt, key=lambda c: -c[2])[:max(4, len(cand))]

    if len(cand) >= 4:
        points = [(c[0], c[1]) for c in cand]
        best_area = -1.0
        best_combo = None
        for combo in combinations(points, 4):
            area = _convex_quad_area(combo)
            if area > best_area:
                best_area = area
                best_combo = combo
        return list(best_combo) if best_combo is not None else None

    if len(cand) == 3 and img is not None:
        points3 = [(c[0], c[1]) for c in cand]
        predicted = _predict_missing_corner(points3)
        weak = _find_weak_marker_near(img, predicted)
        if weak is not None:
            return points3 + [weak]

    return None


def _predict_missing_corner(points):
    """既知の3点から、盤の長方形を仮定して残り1点の位置を予測する。

    長方形の対角線は中点を共有するため、隣接2点と接する「直角の頂点」をPとし、
    残りの2点をA, Bとすると、求める4点目 = A + B - P になる。直角の頂点は、他の2点への
    ベクトルがもっとも直交に近い（内積の絶対値がもっとも小さい）点として判定する。
    """
    pts = [np.array(p, dtype=float) for p in points]
    best_idx, best_dot = 0, None
    for i in range(3):
        p, a, b = pts[i], pts[(i + 1) % 3], pts[(i + 2) % 3]
        v1, v2 = a - p, b - p
        n1, n2 = np.linalg.norm(v1), np.linalg.norm(v2)
        if n1 < 1e-6 or n2 < 1e-6:
            continue
        dot = abs(np.dot(v1, v2) / (n1 * n2))
        if best_dot is None or dot < best_dot:
            best_dot, best_idx = dot, i
    right_angle_pt = pts[best_idx]
    others = [pts[(best_idx + 1) % 3], pts[(best_idx + 2) % 3]]
    predicted = others[0] + others[1] - right_angle_pt
    return float(predicted[0]), float(predicted[1])


def _find_weak_marker_near(img, point, search_radius=220, min_area=40):
    """強い赤丸が3点しか見つからない場合のフォールバック：長方形補完で予測した位置の
    近傍だけを狭く再探索し、通常のモルフォロジー処理・面積下限（200px）を生き残れない
    ほど小さい/断片化した赤い塊でも採用する。

    色のHSVしきい値（RED_LOWER/UPPER）自体は変更せず、探索範囲を予測位置周辺のごく狭い
    矩形に限定しているため、画面の他の場所に写り込んだ無関係な赤い物体（駒袋等）を新たに
    誤検出するリスクは増やさない——実際の駒袋写り込み写真（calib_20260622_110026_845991.jpg）
    で検証済み：駒袋自体は巨大な赤色塊として検出されるが、4隅から離れた場所にあるため
    この狭い探索矩形には入らない。
    """
    h, w = img.shape[:2]
    cx, cy = point
    x0, x1 = int(max(0, cx - search_radius)), int(min(w, cx + search_radius))
    y0, y1 = int(max(0, cy - search_radius)), int(min(h, cy + search_radius))
    if x1 <= x0 or y1 <= y0:
        return None
    crop = img[y0:y1, x0:x1]
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    mask = cv2.bitwise_or(cv2.inRange(hsv, RED_LOWER1, RED_UPPER1),
                           cv2.inRange(hsv, RED_LOWER2, RED_UPPER2))
    cnts, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    best = None
    for c in cnts:
        a = cv2.contourArea(c)
        if a < min_area:
            continue
        (x, y), _ = cv2.minEnclosingCircle(c)
        if best is None or a > best[0]:
            best = (a, x0 + x, y0 + y)
    if best is None:
        return None
    return best[1], best[2]


def calibrate_from_image(img):
    """画像から赤丸4個を自動検出してキャリブレーション行列を計算する。
    検出できなければNoneを返す（呼び出し側は人間の4隅タップにフォールバックする）。

    2026-06-22、UAT課題①：4点のうち3点しか強い候補が見つからない場合でも、
    select_board_corners内の救済ロジック（_find_weak_marker_near）に賭けてみるため、
    最低3点あれば先に進む（従来は4点未満で即None）。
    """
    circles = detect_red_circles(img)
    if len(circles) < 3:
        return None
    corners = select_board_corners(circles, img.shape, img=img)
    if corners is None:
        return None
    return compute_calib(order_points(corners))


# ===== 座標・差分・指し手判定 =====
def square_to_rc(sq):
    col = 8 - (sq // 9)
    row = sq % 9
    return row, col


def rc_to_square(row, col):
    """square_to_rcの逆変換（5回目UAT課題④：対局再開時の盤面復元用）"""
    return (8 - col) * 9 + row


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


def compare_to_board(recognized, board):
    """recognizedを任意の盤面（boardのboard_to_label_grid）と比較する。

    新規対局の確認は常にshogi.Board()（初期配置）が比較対象だったが、5回目UAT課題④
    （対局再開機能）により、中断局のKIFから再現した盤面が比較対象になるケースが
    増えたため、`compare_to_initial`を一般化した（旧名は廃止、呼び出し側で
    `compare_to_board(recognized, shogi.Board())`と書けば同じ）。

    キャリブレーションは常に「答えが分かっている既知の局面」に対して行うため、
    認識結果が一致するかどうかは人間が9x9を目視確認しなくてもプログラム側で判定できる
    （2026-06-21、実機テストでのユーザー指摘により、キャリブレーション確認UIを
    「人間が見て判断」から「プログラムが自動判定」に変更）。

    Returns: [(row, col, expected, got), ...]  空リストなら完全一致。
    """
    expected_grid = board_to_label_grid(board)
    mismatches = []
    for r in range(9):
        for c in range(9):
            if recognized[r][c] != expected_grid[r][c]:
                mismatches.append((r, c, expected_grid[r][c], recognized[r][c]))
    return mismatches


def label_grid_to_board(grid, reference_board):
    """9x9認識結果(grid)を盤上の駒配置として復元したshogi.Boardを作る
    （5回目UAT課題④：認識結果がreference_boardと不一致でも、人間が「このまま進める」を
    選んだ場合に使う——以後の対局はこの盤面を正として指し手判定する）。

    手番・持ち駒（駒台）はreference_boardから引き継ぐ。駒台はそもそも写真からは
    視覚的に読み取らない既存方針（captured-piece trackingはBoard状態の差分のみで行う）
    のため、写真からは判定できない。reference_boardは新規対局なら常にshogi.Board()
    （持ち駒なし）、対局再開ならKIFから再現した盤面（持ち駒あり）になる。
    """
    board = shogi.Board()
    board.clear()
    for r in range(9):
        for c in range(9):
            label = grid[r][c]
            if label == "empty":
                continue
            color_prefix, piece_name = label.split("_", 1)
            piece_type = LABEL_TO_PIECE.get(piece_name)
            if piece_type is None:
                continue
            color = shogi.BLACK if color_prefix == "sente" else shogi.WHITE
            board.set_piece_at(rc_to_square(r, c), shogi.Piece(piece_type, color))
    board.turn = reference_board.turn
    for color in (shogi.BLACK, shogi.WHITE):
        board.pieces_in_hand[color] = reference_board.pieces_in_hand[color].copy()
    return board


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
# 2026-06-22、UAT課題④：TTS読み上げ用のひらがな読み（KIF表記用のPIECE_JAとは別管理）。
# 「歩」をAndroidのTTSエンジンに渡すと「ぽ」と読み上げる事象が報告されたため、
# 漢字をそのまま読み上げに渡すのをやめ、将棋用語として確定しているひらがな読みを使う。
PIECE_YOMI = {'p': 'ふ', 'l': 'きょう', 'n': 'けい', 's': 'ぎん', 'g': 'きん', 'b': 'かく',
              'r': 'ひ', 'k': 'ぎょく',
              '+p': 'と', '+l': 'なりきょう', '+n': 'なりけい', '+s': 'なりぎん',
              '+b': 'うま', '+r': 'りゅう'}
_FILE_MAP = {'a': 1, 'b': 2, 'c': 3, 'd': 4, 'e': 5, 'f': 6, 'g': 7, 'h': 8, 'i': 9}


def _square_ja(sqn):
    return f"{FILE_JA[int(sqn[0])]}{RANK_JA[_FILE_MAP[sqn[1]]]}"


def move_to_text(board, move):
    """boardはmoveを指す前の局面。(kif_text, speech_text)を返す。
    kif_textはKIF用（移動元の半角数字つき）、speech_textはTTS読み上げ用（移動元なし）。
    """
    dst_ja = _square_ja(shogi.SQUARE_NAMES[move.to_square])
    if move.drop_piece_type:
        symbol = shogi.PIECE_SYMBOLS[move.drop_piece_type]
        pj = PIECE_JA.get(symbol, '?')
        py = PIECE_YOMI.get(symbol, pj)
        kif_text = f"{dst_ja}{pj}打"
        speech_text = f"{dst_ja}{py}打"
        return kif_text, speech_text

    src = shogi.SQUARE_NAMES[move.from_square]
    pc = board.piece_at(move.from_square)
    symbol = shogi.PIECE_SYMBOLS[pc.piece_type] if pc else '?'
    pj = PIECE_JA.get(symbol, '?')
    py = PIECE_YOMI.get(symbol, pj)
    pr = "成" if move.promotion else ""
    kif_text = f"{dst_ja}{pj}{pr}({src[0]}{_FILE_MAP[src[1]]})"
    speech_text = f"{dst_ja}{py}{pr}"
    return kif_text, speech_text


def kif_time_field(this_move_seconds, total_seconds):
    """KIFの消費時間欄"(MM:SS/HH:MM:SS)"を作る（5回目UAT課題②）。

    厳密な計測ではなく、サーバが各リクエストを受け取った時刻同士の差分から
    出す「おおよそ」の値（要件通り、厳密でなくてよい）。負値・異常値が来ても
    クラッシュしないよう0に丸める。
    """
    this_move_seconds = max(0, int(this_move_seconds))
    total_seconds = max(0, int(total_seconds))
    m, s = divmod(this_move_seconds, 60)
    h, rem = divmod(total_seconds, 3600)
    mm, ss = divmod(rem, 60)
    return f"({m:02d}:{s:02d}/{h:02d}:{mm:02d}:{ss:02d})"
