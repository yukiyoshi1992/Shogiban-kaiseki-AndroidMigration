"""
サーバ内メモリで保持する「現在の対局」のセッション状態（シングルトン）。

サーバ要件「同時に1局のみ・状態はリクエストごとではなくサーバが保持」（サーバ要件シート）と、
アーキテクチャ検討.md 3節の状態機械を実装する。

状態遷移: idle → ready → playing → finishing → idle
  - idle:      対局なし。/calibration/photo を受け付けられる。
  - ready:     キャリブレーション写真を認識済みで人間の確認待ち（前PJStreamlit版の"ready"と同じ）。
               /calibration/photo を再送すれば何度でもやり直せる（4隅タップのやり直し）。
  - playing:   対局中。/move を受け付ける。
  - finishing: /game/end でKIFを確定する処理中（同期処理のため実質一瞬で idle に戻る）。

注: アーキテクチャ検討.md記載の"calibrating（撮影・4点指定中）"はアプリ側ローカルなUI操作
（撮影・タップ）の段階であり、サーバにリクエストが来るのは常にその後（写真+4点が揃った時点）
なので、サーバの状態としては明示的に持たず idle→ready の1ステップで表現する。
"""

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional

import shogi


class GameState(str, Enum):
    IDLE = "idle"
    READY = "ready"
    PLAYING = "playing"
    FINISHING = "finishing"


@dataclass
class PendingCalibration:
    matrix: "object"  # np.ndarray（透視変換行列）
    recognized: list  # 9x9ラベルグリッド（人間確認用）


@dataclass
class GameSession:
    state: GameState = GameState.IDLE
    pending_calibration: Optional[PendingCalibration] = None
    calib_matrix: "object" = None
    board: Optional["shogi.Board"] = None
    moves_usi: list = field(default_factory=list)
    kif_path: Optional[Path] = None
    game_id: Optional[str] = None

    def reset(self):
        self.state = GameState.IDLE
        self.pending_calibration = None
        self.calib_matrix = None
        self.board = None
        self.moves_usi = []
        self.kif_path = None
        self.game_id = None


# プロセス内シングルトン（サーバ要件：同時1局・状態はサーバが保持）
session = GameSession()
