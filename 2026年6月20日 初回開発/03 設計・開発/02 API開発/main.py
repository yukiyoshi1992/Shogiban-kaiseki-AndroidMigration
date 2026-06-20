"""最小構成のAPIサーバ。今はAndroidアプリ→サーバの通信経路を確認するためだけの
ダミーエンドポイント（写真を受け取って保存するだけ）。盤面解析ロジックは未統合。
"""
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, UploadFile

app = FastAPI()

UPLOAD_DIR = Path(__file__).parent / "received_photos"
UPLOAD_DIR.mkdir(exist_ok=True)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/photo")
async def receive_photo(file: UploadFile) -> dict[str, str]:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = UPLOAD_DIR / f"{timestamp}_{file.filename}"
    dest.write_bytes(await file.read())
    return {"status": "ok", "saved_as": dest.name}
