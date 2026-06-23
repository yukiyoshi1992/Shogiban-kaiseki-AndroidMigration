# -*- coding: utf-8 -*-
"""実際の保存済み写真でエクスポート済みモデル(.ptl)の健全性を確認するPoC。

正確な4隅キャリブレーション座標は使わず（タイミング・健全性確認が目的のため、
盤面認識の正解不正解は問わない）、写真を900x900にリサイズしただけの画像を
「盤面っぽい入力」として両モデルに通し、(a)クラッシュしないか、(b)元モデルと
trace済みモデルの出力が一致するか、を確認する。
"""
import time
from pathlib import Path

import cv2
import numpy as np
import torch
import torch.nn as nn
from PIL import Image
from torchvision import models, transforms

BASE_DIR = Path(__file__).parent
NUM_CLASSES = 29
GRID_SIZE = 9
CELL_PX = 100
WARP_SIDE = CELL_PX * GRID_SIZE

_transform = transforms.Compose([
    transforms.Resize((224, 224)), transforms.ToTensor(),
    transforms.Normalize([0.485, 0.456, 0.406], [0.229, 0.224, 0.225])])


def load_model(model_path: Path) -> nn.Module:
    model = models.mobilenet_v2(weights=None)
    in_f = model.classifier[1].in_features
    model.classifier = nn.Sequential(
        nn.Dropout(0.3), nn.Linear(in_f, 256), nn.ReLU(),
        nn.Dropout(0.2), nn.Linear(256, NUM_CLASSES))
    model.load_state_dict(torch.load(model_path, map_location="cpu"))
    model.eval()
    return model


def make_batch_from_image(warped):
    tensors = []
    for row in range(GRID_SIZE):
        for col in range(GRID_SIZE):
            x1, y1 = col * CELL_PX, row * CELL_PX
            cell = warped[y1:y1 + CELL_PX, x1:x1 + CELL_PX]
            pil = Image.fromarray(cv2.cvtColor(cell, cv2.COLOR_BGR2RGB))
            tensors.append(_transform(pil))
    return torch.stack(tensors)


def main():
    photo_path = Path(
        "//YukiYoshiNAS/Shogiban-kaiseki-appli/00 runtime/server/runtime/photos/"
        "calib_20260621_135539_120137.jpg"
    )
    print(f"実写真を読み込み: {photo_path}")
    img = cv2.imdecode(np.fromfile(str(photo_path), dtype=np.uint8), cv2.IMREAD_COLOR)
    warped = cv2.resize(img, (WARP_SIDE, WARP_SIDE))
    batch = make_batch_from_image(warped)
    print(f"入力バッチ shape: {batch.shape}")

    model = load_model(BASE_DIR / "models" / "best_model.pth")
    ptl_path = BASE_DIR / "exported" / "board_classifier.ptl"
    # torch.jit.loadのネイティブfopenもNAS UNC+日本語パスを直接読めない
    # （export時の_save_for_lite_interpreterと同じ既知の問題）。ローカルに
    # コピーしてから読み込む。
    import shutil
    import tempfile
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_ptl = Path(tmpdir) / "board_classifier.ptl"
        shutil.copy(str(ptl_path), str(tmp_ptl))
        loaded = torch.jit.load(str(tmp_ptl))
    loaded.eval()

    with torch.no_grad():
        t0 = time.perf_counter()
        out_orig = model(batch)
        t_orig = time.perf_counter() - t0

        t0 = time.perf_counter()
        out_ptl = loaded(batch)
        t_ptl = time.perf_counter() - t0

    print(f"\n元モデル推論時間: {t_orig:.3f}秒")
    print(f".ptl推論時間: {t_ptl:.3f}秒")
    print(f"出力の最大絶対誤差: {(out_orig - out_ptl).abs().max().item():.2e}")
    print(f"クラス予測が完全一致: {(out_orig.argmax(1) == out_ptl.argmax(1)).all().item()}")


if __name__ == "__main__":
    main()
