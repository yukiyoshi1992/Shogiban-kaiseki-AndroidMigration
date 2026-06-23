# -*- coding: utf-8 -*-
"""モデル変換の実現性検証PoC（要件定義前の事前調査）。

00 runtime/server/recognition.py の load_model と同じモデル構造を、独立した
複製として再定義する（既存ソースは変更しない・新しい環境で開発する、というPJの
前提に従い、importで本番コードに依存させず構造だけ複製する）。

検証内容：学習済みモデル（MobileNetV2＋カスタム分類ヘッド）を、Android実機上で
動かせる形式（TorchScript Lite Interpreter形式 .ptl）にエクスポートできるかどうか。
できれば、エクスポート後の推論結果が元のPyTorchモデルと数値的に一致するかも確認する。
"""
import time
from pathlib import Path

import torch
import torch.nn as nn
from torchvision import models

BASE_DIR = Path(__file__).parent
NUM_CLASSES = 29


def load_model(model_path: Path) -> nn.Module:
    model = models.mobilenet_v2(weights=None)
    in_f = model.classifier[1].in_features
    model.classifier = nn.Sequential(
        nn.Dropout(0.3), nn.Linear(in_f, 256), nn.ReLU(),
        nn.Dropout(0.2), nn.Linear(256, NUM_CLASSES))
    model.load_state_dict(torch.load(model_path, map_location="cpu"))
    model.eval()
    return model


def main():
    model_path = BASE_DIR / "models" / "best_model.pth"
    print(f"モデル読み込み中: {model_path}")
    model = load_model(model_path)
    print("読み込み完了。")

    # 81マス分をまとめて1バッチで推論する現行サーバ実装(predict_board)と同じ形にする。
    dummy_batch = torch.randn(81, 3, 224, 224)

    print("\n--- 元のPyTorchモデルで推論（基準値） ---")
    with torch.no_grad():
        t0 = time.perf_counter()
        ref_out = model(dummy_batch)
        ref_preds = ref_out.max(1)[1]
        print(f"推論時間: {time.perf_counter() - t0:.3f}秒（PCのCPU、ダミー入力）")

    print("\n--- torch.jit.trace でTorchScript化 ---")
    try:
        traced = torch.jit.trace(model, dummy_batch)
        print("trace成功。")
    except Exception as e:
        print(f"trace失敗: {e}")
        return

    print("\n--- optimize_for_mobile でモバイル最適化 ---")
    # 重要：実際に検証した結果、optimize_for_mobile はこのモデル（MobileNetV2＋
    # カスタム分類ヘッド）に対して数値的に大きく異なる出力を生成することが判明した
    # （ref_out範囲[-41,28]に対し平均絶対誤差23.5、最大誤差39.9——単純なConv+BN融合の
    # 浮動小数点誤差では説明できない大きさ）。torch.jit.trace単体（最適化なし）は
    # 完全一致（誤差0.0）したため、原因はoptimize_for_mobile固有。Lite Interpreter自体も
    # PyTorch公式によりExecuTorchへの移行が推奨されアナウンスされている（実行時に
    # DeprecationWarningが出る）。これらの理由から、ここではoptimize_for_mobileを
    # 適用せず、traceしたモデルをそのまま保存する（モバイル特有の演算融合による高速化は
    # 受けられないが、正しさが確認できているため）。要件定義ドキュメント参照。
    optimized = traced

    out_path = BASE_DIR / "exported" / "board_classifier.ptl"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    # NAS（UNCパス・日本語パス）に対してtorchのネイティブ保存APIを直接使うと
    # "Parent directory does not exist"（実際には存在する）という見せかけのエラーで
    # 失敗する——このプロジェクトで既知の「NAS共有はネイティブファイルAPIと相性が悪い」
    # 問題の一種（CLAUDE.mdのcv2.imread/imwrite注意と同根）。ローカルの一時ファイルに
    # 保存してから通常のファイルコピーでNAS側に移す回避策を取る。
    import shutil
    import tempfile
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir) / "board_classifier.ptl"
        optimized._save_for_lite_interpreter(str(tmp_path))
        shutil.copy(str(tmp_path), str(out_path))
    size_mb = out_path.stat().st_size / (1024 * 1024)
    print(f"\n保存完了: {out_path}（{size_mb:.2f} MB）")

    print("\n--- エクスポート後モデル（trace済み、最適化なし）が元モデルと一致するか確認 ---")
    with torch.no_grad():
        t0 = time.perf_counter()
        opt_out = optimized(dummy_batch)
        print(f"推論時間（trace後、PCのCPU）: {time.perf_counter() - t0:.3f}秒")
    opt_preds = opt_out.max(1)[1]
    matches = (ref_preds == opt_preds).all().item()
    max_abs_diff = (ref_out - opt_out).abs().max().item()
    print(f"クラス予測が完全一致: {matches}")
    print(f"出力テンソルの最大絶対誤差: {max_abs_diff:.2e}")


if __name__ == "__main__":
    main()
