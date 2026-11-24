"""
Gộp toàn bộ tài liệu trong thư mục thongtin/ thành 2 file cho bot:
  - sales_playbook.md      : "bộ não" nạp vào system prompt của Gemini
  - known_image_urls.txt   : whitelist URL ảnh hợp lệ (bot chỉ được gửi ảnh trong danh sách này)

Xử lý:
  - quy_trinh_sale.xlsx  (cột Tình huống / Hướng dẫn xử lý / Lưu ý)  -> các khối tình huống
  - thong_tin.xlsx       (cột Câu hỏi / Câu trả lời)                 -> mục FAQ
  - thong_tin_cho_bot.docx                                           -> nguyên tắc thực thi

Chạy:  python build_knowledge.py
"""
from __future__ import annotations

import os
import re
import zipfile

import openpyxl

SRC_DIR = "thongtin"
PLAYBOOK_PATH = "sales_playbook.md"
URL_RE = re.compile(r"https?://[^\s\"'<>)\]]+\.(?:jpg|jpeg|png|gif|webp)", re.IGNORECASE)


def clean(text) -> str:
    """Chuẩn hóa text ô Excel: bỏ _x000d_, gom xuống dòng, giữ [BREAK]."""
    if text is None:
        return ""
    s = str(text).replace("_x000d_", "\n").replace("\r", "\n")
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def rows_of(path: str):
    wb = openpyxl.load_workbook(path, data_only=True)
    ws = wb.worksheets[0]
    return [[clean(c) for c in row] for row in ws.iter_rows(values_only=True)]


def build_scenarios(path: str) -> str:
    rows = rows_of(path)
    out = ["## QUY TRÌNH TƯ VẤN THEO TÌNH HUỐNG", ""]
    for r in rows[1:]:  # bỏ header
        situation = r[0] if len(r) > 0 else ""
        guide = r[1] if len(r) > 1 else ""
        note = r[2] if len(r) > 2 else ""
        if not situation and not guide:
            continue
        out.append(f"### TÌNH HUỐNG: {situation}")
        out.append("")
        if guide:
            out.append("**Cách xử lý:**")
            out.append(guide)
            out.append("")
        if note:
            out.append("**Lưu ý bắt buộc:**")
            out.append(note)
            out.append("")
    return "\n".join(out)


def build_faq(path: str) -> str:
    rows = rows_of(path)
    out = ["## CÂU HỎI THƯỜNG GẶP (FAQ) — trả lời bám sát nội dung dưới đây", ""]
    for r in rows[1:]:
        q = r[0] if len(r) > 0 else ""
        a = r[1] if len(r) > 1 else ""
        if not q:
            continue
        out.append(f"**HỎI: {q}**")
        out.append(f"ĐÁP: {a}")
        out.append("")
    return "\n".join(out)


def build_rules(path: str) -> str:
    with zipfile.ZipFile(path) as z:
        xml = z.read("word/document.xml").decode("utf-8", "ignore")
    text = re.sub(r"</w:p>", "\n", xml)
    text = re.sub(r"<[^>]+>", "", text)
    import html

    text = html.unescape(text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return "## NGUYÊN TẮC THỰC THI CHUNG (BẮT BUỘC)\n\n" + text


def _scenario_file() -> str:
    """Dùng quy_trinh_sale.xlsx — kịch bản chính, chỉ xin SĐT."""
    return os.path.join(SRC_DIR, "quy_trinh_sale.xlsx")


def build_info_images(path: str) -> str:
    """
    Ảnh "thông tin lớp học" (Học phí KHTN/Lý, Nội quy, Thông tin) — upload riêng bằng
    upload_info_images.py (không nằm trong quy_trinh_sale.xlsx/thong_tin.xlsx), lưu URL vào
    info_image_url_map.json với khóa "Offline|Học phí KHTN" v.v. Chỉ gửi khi khách HỎI HỌC PHÍ,
    chọn đúng ảnh theo lớp (KHTN cấp 2 lớp 6-9 / Vật Lý cấp 3 lớp 10-12) và hình thức đã biết.

    Sinh sẵn 4 "mẫu câu bắt buộc" (đủ text + [BREAK] + URL) — giống hệt định dạng các tình huống
    khác trong kịch bản — để AI COPY NGUYÊN VĂN thay vì chỉ tham khảo 1 danh sách URL rời rạc
    (danh sách rời rạc từng khiến AI nói "em gửi chị tham khảo ảnh ạ" nhưng quên dán URL thật).
    """
    import json as _json

    if not os.path.exists(path):
        return ""
    url_map = _json.load(open(path, encoding="utf-8"))
    if not url_map:
        return ""

    def url_of(hinh_thuc: str, loai: str) -> str:
        return url_map.get(f"{hinh_thuc}|{loai}", "")

    combos = [
        ("Offline", "Học phí KHTN", "KHTN cấp 2 (lớp 6, 7, 8, 9), học TRỰC TIẾP"),
        ("Offline", "Học phí Lý", "Vật Lý cấp 3 (lớp 10, 11, 12), học TRỰC TIẾP"),
        ("Online", "Học phí KHTN", "KHTN cấp 2 (lớp 6, 7, 8, 9), học ONLINE"),
        ("Online", "Học phí Lý", "Vật Lý cấp 3 (lớp 10, 11, 12), học ONLINE"),
    ]

    out = [
        "## ẢNH THÔNG TIN LỚP HỌC (chỉ dùng khi khách HỎI VỀ HỌC PHÍ)",
        "",
        "KHÔNG chủ động gửi các ảnh này ở bước khác. Khi khách hỏi học phí/giá, xác định ĐÚNG 1 trong 4",
        "tình huống dưới đây (dựa vào lớp + hình thức học ĐÃ BIẾT trong hội thoại — nếu chưa biết thì hỏi",
        "trước khi gửi), rồi dùng NGUYÊN VĂN mẫu câu tương ứng (giữ nguyên [BREAK] và URL):",
        "",
    ]
    for hinh_thuc, hoc_phi_loai, mo_ta in combos:
        hp = url_of(hinh_thuc, hoc_phi_loai)
        nq = url_of(hinh_thuc, "Nội quy")
        tt = url_of(hinh_thuc, "Thông tin")
        if not (hp and nq and tt):
            continue
        out.append(f"TÌNH HUỐNG — {mo_ta}:")
        out.append('"Dạ em gửi chị tham khảo thông tin lớp học, nội quy và học phí của trung tâm ạ:')
        out.append("[BREAK]")
        out.append(tt)
        out.append("[BREAK]")
        out.append(nq)
        out.append("[BREAK]")
        out.append(hp + '"')
        out.append("")
    return "\n".join(out)


def main() -> None:
    scen_file = _scenario_file()
    print(f"Nguồn kịch bản: {os.path.basename(scen_file)}")
    scenarios = build_scenarios(scen_file)
    faq = build_faq(os.path.join(SRC_DIR, "thong_tin.xlsx"))
    rules = build_rules(os.path.join(SRC_DIR, "thong_tin_cho_bot.docx"))
    info_images = build_info_images("info_image_url_map.json")

    playbook = "\n\n".join(
        p for p in ["# KỊCH BẢN SALE — TRUNG TÂM VẬT LÝ SIÊU NHẨM", rules, scenarios, faq, info_images] if p
    )

    # Thay link ảnh S3 (preny) bằng link đã upload lên Pancake (content.pancake.vn), nếu có.
    import json as _json

    if os.path.exists("image_url_map.json"):
        url_map = _json.load(open("image_url_map.json", encoding="utf-8"))
        replaced = 0
        for old, new in url_map.items():
            if new and old in playbook:
                playbook = playbook.replace(old, new)
                replaced += 1
        print(f"Đã thay {replaced} link ảnh S3 -> Pancake.")

    with open(PLAYBOOK_PATH, "w", encoding="utf-8") as f:
        f.write(playbook)

    # Whitelist URL ảnh: quét toàn bộ playbook
    urls = sorted(set(URL_RE.findall(playbook)))
    with open("known_image_urls.txt", "w", encoding="utf-8") as f:
        f.write("\n".join(urls))

    print(f"Đã tạo '{PLAYBOOK_PATH}' ({len(playbook)} ký tự).")
    print(f"Đã tạo 'known_image_urls.txt' ({len(urls)} URL ảnh).")


if __name__ == "__main__":
    main()
