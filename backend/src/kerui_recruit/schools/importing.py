"""学校参考数据导入：JSON / CSV 解析、校验与幂等 upsert。

不依赖 HTTP，供 ``api/schools.py`` 调用。导入按归一化名称幂等：
已存在的学校合并标签与别名、刷新 QS 排名字段，避免重复写入。
"""
from __future__ import annotations

import csv
import io

from sqlalchemy import select
from sqlalchemy.orm import Session

from kerui_recruit.db.models import School
from kerui_recruit.schools.reference import normalize_school_name

ALLOWED_TAGS = {"985", "211", "双一流", "普本", "大专", "海外"}

IMPORT_FIELDS = (
    "canonical_name", "aliases", "country_region", "institution_type",
    "tags", "qs_year", "qs_rank_start", "qs_rank_end", "rank_display",
)

CSV_TEMPLATE = (
    "canonical_name,aliases,country_region,institution_type,tags,qs_year,qs_rank_start,qs_rank_end,rank_display\n"
    "斯坦福大学,\"Stanford University;Stanford\",美国,海外院校,海外,2026,1,5,1-5\n"
)


def _as_str_list(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [part.strip() for part in value.replace("；", ";").replace("，", ",").split(";") if part.strip()]
    if isinstance(value, (list, tuple, set)):
        return [str(v).strip() for v in value if str(v).strip()]
    return [str(value).strip()]


def _as_int(value: object, field: str) -> tuple[int | None, str | None]:
    if value is None or value == "":
        return None, None
    try:
        return int(value), None
    except (TypeError, ValueError):
        return None, f"{field} 必须是整数"


def validate_record(record: dict) -> tuple[dict | None, str | None]:
    canonical_name = str(record.get("canonical_name") or "").strip()
    if not canonical_name:
        return None, "canonical_name 不能为空"
    normalized_name = normalize_school_name(canonical_name)
    if not normalized_name:
        return None, f"canonical_name 归一化后为空：{canonical_name!r}"

    qs_year, error = _as_int(record.get("qs_year"), "qs_year")
    if error:
        return None, error
    if qs_year is not None and not (2000 <= qs_year <= 2100):
        return None, "qs_year 必须在 2000~2100 之间"

    qs_rank_start, error = _as_int(record.get("qs_rank_start"), "qs_rank_start")
    if error:
        return None, error
    qs_rank_end, error = _as_int(record.get("qs_rank_end"), "qs_rank_end")
    if error:
        return None, error
    for rank in (qs_rank_start, qs_rank_end):
        if rank is not None and rank < 1:
            return None, "QS 排名必须 >= 1"
    if qs_rank_start is not None and qs_rank_end is not None and qs_rank_start > qs_rank_end:
        return None, "qs_rank_start 不能大于 qs_rank_end"

    tags = _as_str_list(record.get("tags"))
    unknown_tags = [tag for tag in tags if tag not in ALLOWED_TAGS]
    if unknown_tags:
        return None, f"不支持的标签：{', '.join(unknown_tags)}"

    return {
        "canonical_name": canonical_name,
        "normalized_name": normalized_name,
        "aliases": _as_str_list(record.get("aliases")),
        "country_region": str(record.get("country_region") or "").strip() or None,
        "institution_type": str(record.get("institution_type") or "").strip() or None,
        "tags": tags,
        "qs_year": qs_year,
        "qs_rank_start": qs_rank_start,
        "qs_rank_end": qs_rank_end,
        "rank_display": str(record.get("rank_display") or "").strip() or None,
    }, None


def import_schools(session: Session, records: list[dict]) -> dict:
    """校验并写入学校记录，返回汇总与逐条错误。"""
    imported = 0
    updated = 0
    errors: list[dict] = []
    for index, record in enumerate(records):
        normalized, error = validate_record(record)
        if error is not None:
            errors.append({"index": index, "error": error})
            continue
        existing = session.scalar(select(School).where(
            School.normalized_name == normalized["normalized_name"]))
        if existing is None:
            session.add(School(
                canonical_name=normalized["canonical_name"],
                normalized_name=normalized["normalized_name"],
                aliases=normalized["aliases"] or None,
                country_region=normalized["country_region"],
                institution_type=normalized["institution_type"],
                tags=normalized["tags"] or None,
                qs_year=normalized["qs_year"],
                qs_rank_start=normalized["qs_rank_start"],
                qs_rank_end=normalized["qs_rank_end"],
                rank_display=normalized["rank_display"],
                source="imported",
            ))
            imported += 1
        else:
            existing.canonical_name = normalized["canonical_name"]
            existing.aliases = sorted(set((existing.aliases or []) + normalized["aliases"])) or None
            existing.country_region = normalized["country_region"] or existing.country_region
            existing.institution_type = normalized["institution_type"] or existing.institution_type
            existing.tags = sorted(set((existing.tags or []) + normalized["tags"])) or None
            existing.qs_year = normalized["qs_year"]
            existing.qs_rank_start = normalized["qs_rank_start"]
            existing.qs_rank_end = normalized["qs_rank_end"]
            existing.rank_display = normalized["rank_display"] or existing.rank_display
            updated += 1
    session.flush()
    return {"imported": imported, "updated": updated, "failed": len(errors), "errors": errors}


def parse_csv(text: str) -> list[dict]:
    """把 CSV 文本解析为记录列表；首行为字段名。"""
    reader = csv.DictReader(io.StringIO(text))
    records: list[dict] = []
    for row in reader:
        records.append({key: (value or "").strip() for key, value in row.items() if key})
    return records
