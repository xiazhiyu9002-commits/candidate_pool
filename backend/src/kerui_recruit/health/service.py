from __future__ import annotations

import shutil

from sqlalchemy import text

from kerui_recruit.api.services import AppServices


class HealthService:
    def __init__(self, services: AppServices) -> None:
        self.services = services

    def check(self) -> dict[str, dict]:
        report: dict[str, dict] = {}

        try:
            with self.services.session_factory() as session:
                session.execute(text("SELECT 1"))
            report["database"] = {"status": "healthy"}
        except Exception:
            report["database"] = {"status": "unhealthy", "message": "数据库连接失败"}

        try:
            self.services.blob_store.root.mkdir(parents=True, exist_ok=True)
            report["blob_store"] = {"status": "healthy"}
        except Exception:
            report["blob_store"] = {"status": "unhealthy", "message": "原件库不可用"}

        try:
            indexed = self.services.search_service.warmup()
            report["search"] = {"status": "healthy", "message": f"{indexed} 个检索块"}
        except Exception:
            report["search"] = {"status": "unhealthy", "message": "检索引擎不可用"}

        try:
            usage = shutil.disk_usage(self.services.blob_store.root)
            free_gb = usage.free / (1024 ** 3)
            report["disk"] = {"status": "healthy", "message": f"剩余 {free_gb:.1f} GB"}
        except Exception:
            report["disk"] = {"status": "unhealthy", "message": "无法读取磁盘空间"}

        return report