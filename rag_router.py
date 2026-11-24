"""
Định tuyến kịch bản THEO LỚP (phương án B thay cho RAG semantic) — PHIÊN BẢN MỚI, TÁCH BIỆT HOÀN TOÀN
khỏi ai_service.py/sales_playbook.md hiện có (không import, không sửa file nào đang chạy).

Thay vì nhét TOÀN BỘ tình huống vào mọi lần gọi AI (như bản hiện tại), file này:
  - Luôn ghép các tình huống "luôn cần" (chào hỏi, reaction, SĐT, báo giá chung, tư vấn chung...).
  - Chỉ ghép THÊM đúng 1 khối tình huống của lớp đang biết/nghi ngờ (known_grade từ lượt trước, hoặc dò
    trực tiếp trong tin nhắn khách bằng từ khóa — y hệt "QUY TẮC NHẬN DIỆN LỚP/KHÓA" đã có trong kịch bản).
  - Chưa biết lớp (lượt đầu, không dò được) -> ghép ĐỦ TẤT CẢ tình huống theo lớp (an toàn, không đoán bừa,
    giống hệt hành vi bản hiện tại).

KHÔNG dùng embedding/vector search: domain có cấu trúc rõ (7 khóa lớp cố định, không phải kho văn bản tự
do), nên định tuyến tất định theo từ khóa đủ chính xác và không có rủi ro "gần đúng"/retrieval-miss như
semantic search — đây là lựa chọn CHỦ ĐÍCH thay cho RAG thật, không phải bản rút gọn của RAG thật.
"""
from __future__ import annotations

import json
import logging
import os
import re

log = logging.getLogger("rag_router")

CHUNKS_PATH = "knowledge_chunks.json"

# Y hệt "QUY TẮC NHẬN DIỆN LỚP/KHÓA" trong kịch bản — dò lớp NGAY TRONG tin nhắn khách (trước khi có
# known_grade từ AI), để lượt ĐẦU TIÊN cũng định tuyến được, không phải đợi tới lượt 2 mới thu hẹp được.
_GRADE_DETECT_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"lớp\s*12|11\s*lên\s*12|2k9\b|2009\b", re.IGNORECASE), "2009"),
    (re.compile(r"lớp\s*11|10\s*lên\s*11|2k10\b|2010\b", re.IGNORECASE), "2010"),
    (re.compile(r"lớp\s*10|9\s*lên\s*10|2k11\b|2011\b", re.IGNORECASE), "2011"),
    (re.compile(r"lớp\s*9\b|8\s*lên\s*9|2k12\b|2012\b", re.IGNORECASE), "2012"),
    (re.compile(r"lớp\s*8\b|7\s*lên\s*8|2k13\b", re.IGNORECASE), "2k13"),
    (re.compile(r"lớp\s*7\b|6\s*lên\s*7|2k14\b", re.IGNORECASE), "2k14"),
    (re.compile(r"lớp\s*6\b|5\s*lên\s*6|2k15\b|2015\b", re.IGNORECASE), "2k15"),
]


def detect_grade_hint(text: str) -> str | None:
    """Dò nhanh 1 khóa lớp trong tin nhắn khách bằng từ khóa (KHÔNG gọi AI) — CHỈ dùng để ĐỊNH TUYẾN kịch
    bản trước khi gọi AI chính, không ảnh hưởng/thay thế "known_grade" thật do AI tự trả về trong kết quả."""
    if not text:
        return None
    for pattern, grade in _GRADE_DETECT_PATTERNS:
        if pattern.search(text):
            return grade
    return None


_chunks_cache: dict | None = None


def _load_chunks() -> dict:
    global _chunks_cache
    if _chunks_cache is None:
        if not os.path.exists(CHUNKS_PATH):
            log.warning("Thiếu '%s' — chạy build_knowledge_chunks.py trước khi dùng bản RAG.", CHUNKS_PATH)
            _chunks_cache = {"rules": "", "scenarios": [], "faq": [], "info_images": ""}
        else:
            with open(CHUNKS_PATH, encoding="utf-8") as f:
                _chunks_cache = json.load(f)
    return _chunks_cache


def _format_scenario(c: dict) -> str:
    out = [f"### TÌNH HUỐNG: {c['situation']}", ""]
    if c.get("guide"):
        out += ["**Cách xử lý:**", c["guide"], ""]
    if c.get("note"):
        out += ["**Lưu ý bắt buộc:**", c["note"], ""]
    return "\n".join(out)


def build_playbook(known_grade: str | None) -> tuple[str, list[str]]:
    """Ghép kịch bản THEO LỚP thay vì toàn bộ.
    Trả về (nội dung kịch bản, danh sách tên tình huống đã đưa vào — để log/debug/so sánh).
    known_grade None hoặc "chưa biết" -> ghép ĐỦ TẤT CẢ tình huống theo lớp (an toàn)."""
    data = _load_chunks()
    scenarios = data.get("scenarios", [])

    always = [c for c in scenarios if not c.get("grades")]
    if known_grade and known_grade != "chưa biết":
        grade_specific = [c for c in scenarios if c.get("grades") and known_grade in c["grades"]]
    else:
        grade_specific = [c for c in scenarios if c.get("grades")]

    included = always + grade_specific
    scenarios_md = "\n".join(_format_scenario(c) for c in included)

    faq_md = "## CÂU HỎI THƯỜNG GẶP (FAQ) — trả lời bám sát nội dung dưới đây\n\n" + "\n".join(
        f"**HỎI: {f['q']}**\nĐÁP: {f['a']}\n" for f in data.get("faq", [])
    )

    parts = [
        data.get("rules", ""),
        "## QUY TRÌNH TƯ VẤN THEO TÌNH HUỐNG\n\n" + scenarios_md,
        faq_md,
        data.get("info_images", ""),
    ]
    playbook = "\n\n".join(p for p in parts if p)
    return playbook, [c["situation"] for c in included]
