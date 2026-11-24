"""
Script SETUP: liệt kê page và tạo page_access_token (dùng lâu dài cho bot).

Dùng Internal API với PANCAKE_ACCESS_TOKEN (JWT đăng nhập cá nhân).
Chạy:  python list_pages.py
       python list_pages.py <page_id>     # tạo page_access_token cho page đó

Sau đó điền PANCAKE_PAGE_ID + PANCAKE_PAGE_ACCESS_TOKEN vào .env.
"""
import sys

import httpx

from config import settings

BASE = settings.pancake_internal_url  # https://pages.fm/api/v1


def list_pages() -> None:
    r = httpx.get(f"{BASE}/pages", params={"access_token": settings.pancake_access_token}, timeout=30)
    r.raise_for_status()
    cat = r.json().get("categorized", {})
    print("=== Các page bạn quản lý ===")
    for group in ("activated", "not_activated"):
        for p in cat.get(group, []):
            print(f"  id={p.get('id'):<22} platform={p.get('platform'):<24} {p.get('name')}")
    print("\nTạo token cho 1 page:  python list_pages.py <page_id>")


def gen_token(page_id: str) -> None:
    r = httpx.post(
        f"{BASE}/pages/{page_id}/generate_page_access_token",
        params={"access_token": settings.pancake_access_token},
        timeout=30,
    )
    r.raise_for_status()
    data = r.json()
    print(f"page_id              = {page_id}")
    print(f"page_access_token    = {data.get('page_access_token')}")
    print("\n-> Dán 2 dòng trên vào .env (PANCAKE_PAGE_ID, PANCAKE_PAGE_ACCESS_TOKEN)")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        gen_token(sys.argv[1])
    else:
        list_pages()
