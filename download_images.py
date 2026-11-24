"""
Trích toàn bộ URL ảnh trong kịch bản sale (các file trong thư mục thongtin/)
và tải về thư mục images/. Sinh manifest images/manifest.csv (url -> file local).

Chạy:  python download_images.py
"""
from __future__ import annotations

import csv
import os
import re
import urllib.request
import zipfile

import openpyxl

SRC_DIR = "thongtin"
OUT_DIR = "images"
URL_RE = re.compile(r"https?://[^\s\"'<>)\]]+\.(?:jpg|jpeg|png|gif|webp)", re.IGNORECASE)


def urls_from_xlsx(path: str) -> set[str]:
    found: set[str] = set()
    wb = openpyxl.load_workbook(path, data_only=True)
    for ws in wb.worksheets:
        for row in ws.iter_rows():
            for c in row:
                if c.value:
                    found.update(URL_RE.findall(str(c.value)))
                if c.hyperlink and c.hyperlink.target:
                    found.update(URL_RE.findall(c.hyperlink.target))
    return found


def urls_from_docx(path: str) -> set[str]:
    found: set[str] = set()
    with zipfile.ZipFile(path) as z:
        for name in z.namelist():
            if name.endswith(".xml") or name.endswith(".rels"):
                text = z.read(name).decode("utf-8", errors="ignore")
                found.update(URL_RE.findall(text))
    return found


def collect_urls() -> set[str]:
    urls: set[str] = set()
    for fn in os.listdir(SRC_DIR):
        p = os.path.join(SRC_DIR, fn)
        if fn.lower().endswith((".xlsx", ".xlsm")):
            urls |= urls_from_xlsx(p)
        elif fn.lower().endswith(".docx"):
            urls |= urls_from_docx(p)
    return urls


def filename_for(url: str, index: int) -> str:
    base = url.split("/")[-1].split("?")[0]
    if not base or "." not in base:
        base = f"image_{index}.jpg"
    return f"{index:02d}_{base}"


def main() -> None:
    os.makedirs(OUT_DIR, exist_ok=True)
    urls = sorted(collect_urls())
    print(f"Tìm thấy {len(urls)} URL ảnh (đã loại trùng).")

    manifest_rows = []
    for i, url in enumerate(urls, 1):
        fname = filename_for(url, i)
        dest = os.path.join(OUT_DIR, fname)
        status = "ok"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = resp.read()
            with open(dest, "wb") as f:
                f.write(data)
            print(f"[{i}/{len(urls)}] OK  {fname}  ({len(data)//1024} KB)")
        except Exception as e:  # noqa: BLE001
            status = f"loi: {e}"
            print(f"[{i}/{len(urls)}] LOI {url} -> {e}")
        manifest_rows.append({"index": i, "url": url, "file": fname, "status": status})

    with open(os.path.join(OUT_DIR, "manifest.csv"), "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=["index", "url", "file", "status"])
        w.writeheader()
        w.writerows(manifest_rows)

    ok = sum(1 for r in manifest_rows if r["status"] == "ok")
    print(f"\nHoàn tất: {ok}/{len(urls)} ảnh tải thành công -> thư mục '{OUT_DIR}/'")
    print(f"Manifest: {OUT_DIR}/manifest.csv")


if __name__ == "__main__":
    main()
