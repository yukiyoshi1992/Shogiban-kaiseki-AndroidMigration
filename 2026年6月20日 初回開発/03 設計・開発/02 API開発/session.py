"""
サーバ内メモリで保持する「現在の対局」のセッション状態（シングルトン）。

サーバ要件「同時に1局のみ・状態はリクエストごとではなくサーバが保持」（サーバ要件シート）と、
アーキテクチャ検討.md 3節の状態機械を実装する。

状態遷移: idle → playing → finishing → idle
  - idle:      対局なし。/calibration/photo を受け付けられる。
  - playing:   対局中。/move を受け付ける。
  - finishing: /game/end でKIFを確定する処理中（同期処理のため実質一瞬で idle に戻る）。

注: アーキテクチャ検討.md記載の"calibrating（撮影・4点指定中）"はアプリ側ローカルなUI操作
（撮影・タップ）の段階であり、サーバにリクエストが来るのは常にその後（写真+4点が揃った時点）
なので、サーバの状態としては明示的に持たず idle→playing の1ステップで表現する。

2026-06-22、"ready"状態は廃止した。元は「9x9を人間が目視確認してOKを押す」ための
/calibration/confirm（別リクエスト）を待つ間の状態だったが、その人間確認UIは
サーバ側の自動判定（matches_initial）に置き換えられて廃止済みで、確認の
ための2回目のリクエスト自体に人間の判断が一切介在しなくなっていた
（かつこの2回目のリクエストが実機で断続的にタイムアウトする事象が確認された）。
/calibration/photo 1回のリクエスト内で対局開始まで完了するよう統合したため、
「写真は認識したが対局はまだ始めていない」という中間状態はもう存在しない。
"""

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional

import shogi


class GameState(str, Enum):
    IDLE = "idle"
    PLAYING = "playing"
    FINISHING = "finishing"


@dataclass
class GameSession:
    state: GameState = GameState.IDLE
    calib_matrix: "object" = None
    board: Optional["shogi.Board"] = None
    moves_usi: list = field(default_factory=list)
    kif_path: Optional[Path] = None
    game_id: Optional[str] = None

    def reset(self):
        self.state = GameState.IDLE
        self.calib_matrix = None
        self.board = None
        self.moves_usi = []
        self.kif_path = None
        self.game_id = None


# プロセス内シングルトン（サーバ要件：同時1局・状態はサーバが保持）
session = GameSession()
