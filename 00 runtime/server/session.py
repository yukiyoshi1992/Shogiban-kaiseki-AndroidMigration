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

2026-06-22追記（UAT課題②）：手動タップ（4隅指定）の直後に限り、「グリッド線が
正しく盤に重なっているか」を人間が目視確認する画面が復活した。これは前回削除した
9x9目視確認（自動判定に置き換え済み）とは別物——盤面認識結果の確認ではなく、
4隅タップ自体の精度（透視変換が合っているか）の確認。この確認待ちの間だけ、
`pending_calib_matrix`に透視変換行列を一時保持する（state自体はidleのまま変えない
——再タップで`/calibration/photo`を呼び直せば単に上書きされるだけで良いため、
専用の状態遷移は不要と判断）。

2026-06-22追記（2回目UAT課題③）：上記のグリッド確認画面を、赤丸自動検出が成功した
場合（matches_initial=true）にも適用するよう拡張した。これに伴い、フィールド名を
`pending_calib_matrix`から`pending_calib_matrix`に変更（手動専用ではなくなったため）。

2026-06-23追記（5回目UAT課題②）：KIFの消費時間欄が常に"00:00/00:00:00"だった
（時間を記録していなかった）ため、`start_time`（対局開始＝キャリブレーション確定→
対局画面遷移の時点）と`last_move_time`（直前の手が記録された時刻、なければ
start_time）を追加した。厳密な計測ではなく、サーバが各リクエストを受け取った
時刻同士の差分から「おおよそ」の消費時間を出す方針（要件通り、厳密な時間は不要）。
"""

from dataclasses import dataclass, field
from datetime import datetime
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
    pending_calib_matrix: "object" = None
    board: Optional["shogi.Board"] = None
    moves_usi: list = field(default_factory=list)
    kif_path: Optional[Path] = None
    game_id: Optional[str] = None
    start_time: Optional[datetime] = None
    last_move_time: Optional[datetime] = None

    def reset(self):
        self.state = GameState.IDLE
        self.calib_matrix = None
        self.pending_calib_matrix = None
        self.board = None
        self.moves_usi = []
        self.kif_path = None
        self.game_id = None
        self.start_time = None
        self.last_move_time = None


# プロセス内シングルトン（サーバ要件：同時1局・状態はサーバが保持）
session = GameSession()
