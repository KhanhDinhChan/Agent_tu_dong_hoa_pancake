"""
Sinh knowledge_chunks.json từ thongtin/ — PHIÊN BẢN RAG (định tuyến kịch bản theo lớp), TÁCH BIỆT HOÀN
TOÀN khỏi build_knowledge.py/sales_playbook.md hiện có (KHÔNG sửa, KHÔNG ghi đè file nào đang chạy —
chỉ ĐỌC lại các hàm thuần build_rules/build_info_images/rows_of/clean từ build_knowledge.py).

Khác với build_knowledge.py (gộp tất cả thành 1 chuỗi .md phẳng nạp mỗi lượt), file này tách kịch bản
theo TỪNG TÌNH HUỐNG (mỗi dòng Excel = 1 chunk) và đánh dấu dòng nào "luôn cần" (chào hỏi, reaction,
SĐT, báo giá chung...) hay dòng nào "theo lớp cụ thể" (map ra grade tag) — để rag_router.py chỉ ghép
đúng phần cần thiết mỗi lượt gọi AI thay vì nhét toàn bộ 15 tình huống vào mọi lần.

Chạy: python build_knowledge_chunks.py
"""
from __future__ import annotations

import json
import os

from build_knowledge import build_info_images, build_rules, rows_of

SRC_DIR = "thongtin"
OUT_PATH = "knowledge_chunks.json"

# Map từ khóa trong cột "Tình huống" (A) -> (các) grade tag cụ thể. Đối chiếu thủ công với nội dung
# quy_trinh_sale.xlsx hiện tại (đã dedup) — dòng nào KHÔNG khớp từ khóa nào bên dưới coi là "luôn cần".
GRADE_BY_SITUATION_KEYWORD: list[tuple[str, list[str]]] = [
    ("LỚP 7-8", ["2k13", "2k14"]),
    ("LỚP 12", ["2009"]),
    ("LỚP 11", ["2010"]),
    ("LỚP 10", ["2011"]),
    ("LỚP 9", ["2012"]),
    ("LỚP 6", ["2k15"]),
]
ALL_GRADE_TAGS = {"2009", "2010", "2011", "2012", "2k13", "2k14", "2k15"}


def classify(situation: str) -> list[str] | None:
    up = situation.upper()
    for keyword, grades in GRADE_BY_SITUATION_KEYWORD:
        if keyword in up:
            return grades
    return None  # None = luôn cần, không thuộc riêng lớp nào


def build_scenario_chunks(path: str) -> list[dict]:
    rows = rows_of(path)
    out = []
    for r in rows[1:]:  # bỏ header
        situation = r[0] if len(r) > 0 else ""
        guide = r[1] if len(r) > 1 else ""
        note = r[2] if len(r) > 2 else ""
        if not situation and not guide:
            continue
        out.append({"situation": situation, "guide": guide, "note": note, "grades": classify(situation)})
    return out


def build_faq_chunks(path: str) -> list[dict]:
    rows = rows_of(path)
    out = []
    for r in rows[1:]:
        q = r[0] if len(r) > 0 else ""
        a = r[1] if len(r) > 1 else ""
        if not q:
            continue
        out.append({"q": q, "a": a})
    return out


def main() -> None:
    rules = build_rules(os.path.join(SRC_DIR, "thong_tin_cho_bot.docx"))
    scenarios = build_scenario_chunks(os.path.join(SRC_DIR, "quy_trinh_sale.xlsx"))
    faq = build_faq_chunks(os.path.join(SRC_DIR, "thong_tin.xlsx"))
    info_images = build_info_images("info_image_url_map.json")

    # Thay link ảnh S3 (preny) -> Pancake (content.pancake.vn), giống hệt build_knowledge.py, để đồng bộ.
    replaced = 0
    if os.path.exists("image_url_map.json"):
        url_map = json.load(open("image_url_map.json", encoding="utf-8"))

        def _replace(s: str) -> str:
            nonlocal replaced
            for old, new in url_map.items():
                if new and old in s:
                    s = s.replace(old, new)
                    replaced += 1
            return s

        rules = _replace(rules)
        for c in scenarios:
            c["guide"] = _replace(c["guide"])
            c["note"] = _replace(c["note"])
        for f in faq:
            f["a"] = _replace(f["a"])
        info_images = _replace(info_images)

    data = {"rules": rules, "scenarios": scenarios, "faq": faq, "info_images": info_images}
    with open(OUT_PATH, "w", encoding="utf-8") as fp:
        json.dump(data, fp, ensure_ascii=False, indent=1)

    always_n = sum(1 for c in scenarios if not c["grades"])
    grade_n = len(scenarios) - always_n
    covered = {g for c in scenarios if c["grades"] for g in c["grades"]}
    missing = ALL_GRADE_TAGS - covered
    print(f"Đã tạo '{OUT_PATH}': {len(scenarios)} tình huống ({always_n} luôn cần, {grade_n} theo lớp), "
          f"{len(faq)} FAQ, thay {replaced} link ảnh.")
    if missing:
        print(f"⚠️  CẢNH BÁO: {len(scenarios)} tình huống KHÔNG khớp đủ khóa lớp — thiếu chunk cho: {sorted(missing)}")
    else:
        print(f"OK: đủ cả {len(ALL_GRADE_TAGS)} khóa lớp đều có chunk riêng.")


if __name__ == "__main__":
    main()
