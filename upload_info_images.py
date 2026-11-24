"""
Upload 8 ảnh "thông tin lớp học" (Học phí KHTN/Lý, Nội quy, Thông tin) trong
images/Lớp Online/ và images/Lớp Offline/ lên Pancake -> lấy content_url thật.

Lưu kết quả vào info_image_url_map.json: {"Online|Học phí KHTN": "https://content.pancake.vn/...", ...}

Chạy: python upload_info_images.py
"""
from __future__ import annotations

import json
import os

import httpx

from config import settings

BASE = settings.pancake_internal_url  # https://pages.fm/api/v1
PAGE_ID = settings.pancake_page_id

FILES = {
    "Offline|Học phí KHTN": "images/Lớp Offline/Học phí KHTN.png",
    "Offline|Học phí Lý": "images/Lớp Offline/Học phí lý.png",
    "Offline|Nội quy": "images/Lớp Offline/Nội quy.png",
    "Offline|Thông tin": "images/Lớp Offline/Thông tin.jpg",
    "Online|Học phí KHTN": "images/Lớp Online/Học phí KHTN.png",
    "Online|Học phí Lý": "images/Lớp Online/Học phí Lý.png",
    "Online|Nội quy": "images/Lớp Online/Nội quy.png",
    "Online|Thông tin": "images/Lớp Online/Thông tin lớp Online.jpg",
}


def upload_one(path: str) -> str:
    with open(path, "rb") as f:
        files = {"file": (os.path.basename(path), f)}
        r = httpx.post(
            f"{BASE}/pages/{PAGE_ID}/contents",
            params={"access_token": settings.pancake_access_token},
            files=files,
            timeout=60,
        )
    r.raise_for_status()
    data = r.json()
    return data.get("content_url") or data.get("url") or ""


def main() -> None:
    result = {}
    for key, path in FILES.items():
        print(f"Đang upload: {key} ({path}) ...")
        try:
            url = upload_one(path)
            result[key] = url
            print(f"  -> {url}")
        except Exception as e:  # noqa: BLE001
            print(f"  LOI: {e}")
    with open("info_image_url_map.json", "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=1)
    print(f"\nDa luu {len(result)}/{len(FILES)} anh -> info_image_url_map.json")


if __name__ == "__main__":
    main()
