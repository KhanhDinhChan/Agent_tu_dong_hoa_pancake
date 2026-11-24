"""Watchdog NGOÀI tiến trình bot — chạy ĐỘC LẬP, riêng biệt (nếu bot chết/treo cứng, watchdog vẫn còn
sống để cứu). Theo dõi file nhịp tim (bot_heartbeat.txt) do chính bot tự ghi mỗi 30s qua
meta_orchestrator.MetaOrchestrator.watchdog_loop(). Nếu nhịp tim không cập nhật quá HEARTBEAT_STALE_SECONDS
— nghĩa là event loop của bot đã treo cứng hoàn toàn, đến mức không tự chạy nổi cả vòng lặp ghi nhịp tim
đơn giản của chính nó, lớp bảo vệ "tự huỷ task treo" bên trong cũng vô dụng — thì tự tìm tiến trình đang
giữ cổng 8100, kill rồi khởi động lại uvicorn từ đầu.

Chạy (mở 1 cửa sổ riêng, để chạy nền suốt, độc lập với bot):
  python watchdog.py
"""
from __future__ import annotations

import logging
import os
import subprocess
import time

import psutil

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s watchdog: %(message)s")
log = logging.getLogger("watchdog")

HEARTBEAT_PATH = "bot_heartbeat.txt"
PORT = 8100
CHECK_INTERVAL_SECONDS = 30
# Bot ghi nhịp tim mỗi 30s -> quá 150s (~5 nhịp bị bỏ lỡ liên tiếp) coi như treo cứng thật, không phải do
# 1 lần ghi file bị chậm ngẫu nhiên.
HEARTBEAT_STALE_SECONDS = 150
# Sau khi tự khởi động lại, bot cần thời gian nạp trang (gọi Pancake API) trước khi vòng lặp nhịp tim kịp
# ghi lần đầu -> tạm ngưng kiểm tra 1 khoảng để tránh restart lặp lại liên tục do chưa kịp có nhịp tim mới.
STARTUP_GRACE_SECONDS = 90


def _heartbeat_age() -> float | None:
    if not os.path.exists(HEARTBEAT_PATH):
        return None
    try:
        with open(HEARTBEAT_PATH, encoding="utf-8") as f:
            ts = float(f.read().strip())
        return time.time() - ts
    except Exception:  # noqa: BLE001
        return None


def _find_pid_on_port(port: int) -> int | None:
    for conn in psutil.net_connections(kind="tcp"):
        if conn.laddr and conn.laddr.port == port and conn.status == psutil.CONN_LISTEN:
            return conn.pid
    return None


def _restart_bot() -> None:
    pid = _find_pid_on_port(PORT)
    if pid:
        log.warning("Kill tiến trình bot cũ (PID=%d) vì nhịp tim đã treo.", pid)
        try:
            psutil.Process(pid).kill()
            time.sleep(2)
        except Exception as e:  # noqa: BLE001
            log.exception("Lỗi khi kill PID %d: %s", pid, e)

    log.warning("Khởi động lại bot...")
    with open("main_meta.log", "a", encoding="utf-8") as log_file:
        subprocess.Popen(
            ["python", "-m", "uvicorn", "main_meta:app", "--port", str(PORT)],
            stdout=log_file,
            stderr=subprocess.STDOUT,
        )


def main() -> None:
    log.info(
        "Watchdog bắt đầu theo dõi %s (kiểm tra mỗi %ds, ngưỡng coi là treo %ds).",
        HEARTBEAT_PATH, CHECK_INTERVAL_SECONDS, HEARTBEAT_STALE_SECONDS,
    )
    just_restarted_at = 0.0
    while True:
        time.sleep(CHECK_INTERVAL_SECONDS)

        if time.time() - just_restarted_at < STARTUP_GRACE_SECONDS:
            continue

        age = _heartbeat_age()
        if age is None:
            log.info("Chưa có file nhịp tim (bot có thể vừa khởi động) -> bỏ qua lần kiểm tra này.")
            continue

        if age > HEARTBEAT_STALE_SECONDS:
            log.error(
                "Nhịp tim cũ %.0fs (> ngưỡng %ds) -> bot coi như đã TREO CỨNG, tự khởi động lại.",
                age, HEARTBEAT_STALE_SECONDS,
            )
            _restart_bot()
            just_restarted_at = time.time()
        else:
            log.info("OK — nhịp tim mới nhất cách đây %.0fs.", age)


if __name__ == "__main__":
    main()
