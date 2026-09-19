"""批量操作服务包。"""
from kerui_recruit.bulk.service import (
    BulkItemResult,
    BulkResult,
    BULK_LIMIT,
    bulk_delete_candidates,
    bulk_delete_cases,
    bulk_delete_jds,
    bulk_download_candidates,
    bulk_force_ocr,
)

__all__ = [
    "BulkItemResult",
    "BulkResult",
    "BULK_LIMIT",
    "bulk_delete_candidates",
    "bulk_delete_cases",
    "bulk_delete_jds",
    "bulk_download_candidates",
    "bulk_force_ocr",
]
