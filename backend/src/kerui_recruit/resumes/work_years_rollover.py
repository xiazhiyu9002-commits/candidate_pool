"""随年份滚动的字段刷新：工作年限、年龄，以及画像文本里的这两个数字。

模型在解析时算出的这些都是**一次性快照**，不会随时间增长。本模块每天重算一次并同步
三处副本：

- ``Candidate.total_years``（候选人列）
- ``revision.parsed_data``（工作年限 / 年龄 / 画像文本，前端展示 + 画像输入哈希）
- 搜索索引（工作年限会进向量文本与关键词文本，年龄只进关键词文本；不重建则会出现
  「显示 15 年、按 15 年筛不出来」）

**画像不做整段重生**（那要调用模型，成本高且会覆盖使用者措辞），只把画像里的总年限与
年龄两个数字就地替换，见 ``resumes/profile_text``。

口径：

- 工作年限 = 最早一段工作的起始月 → 当前月（连续口径，含中间空窗）。
- 年龄 = 基准年龄 + (今年 − 基准年)。**基准在首次刷新时建立，年龄当次不变**，也就是
  不补历史欠账、从本次升级起每年 +1。使用者手工改过年龄后基准会被重置（见 API 侧），
  因此手工值之后同样逐年增长。

同一候选人若有多条当前版本（历史遗留状态），列值只按创建时间最新的一条写入一次。
写入按 ``commit_batch_size`` 分批提交：首日口径校正可能命中数百位候选人，放在一个
事务里会长时间持有 SQLite 写锁，把并发的前端写入打成 "database is locked"。
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from kerui_recruit.db.models import Candidate, ResumeDocument, ResumeRevision
from kerui_recruit.resumes.profile_text import refresh_profile_fields
from kerui_recruit.resumes.tenure import compute_total_years
from kerui_recruit.search.sync import enqueue_sync

logger = logging.getLogger(__name__)

SHANGHAI = timezone(timedelta(hours=8))

# 单个写事务最多处理多少条：首日口径校正可能命中数百位候选人，若放在一个事务里会
# 长时间持有 SQLite 写锁（`busy_timeout` 只有 5 秒），把并发的前端写入打成
# "database is locked"。分批后单事务回到毫秒级。
DEFAULT_COMMIT_BATCH_SIZE = 200

# 不参与滚动的候选人状态：与画像批量回填、搜索索引的范围保持一致
# （索引快照对这两种状态直接返回 None，刷新它们只会产生无意义的索引同步）。
_REFRESHABLE_STATUSES = ("ARCHIVED", "PENDING_REVIEW")

AGE_BASELINE_FIELD = "age_baseline"
AGE_BASELINE_YEAR_FIELD = "age_baseline_year"


def _refresh_scope():
    """滚动刷新与「可用版本」判定共用的过滤条件，避免两处口径漂移。"""
    return (
        ResumeRevision.is_current.is_(True),
        ResumeRevision.status == "READY",
        Candidate.deleted_at.is_(None),
        Candidate.status.not_in(_REFRESHABLE_STATUSES),
    )


def _as_decimal(value) -> Decimal | None:
    """``parsed_data`` 里的年限是 JSON 字符串（如 ``"5.0"``），统一按 Decimal 比较。"""
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (ArithmeticError, ValueError):
        return None


def _as_int(value) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def established_age_baseline(age: int, year: int) -> dict:
    """建立／重置年龄基准：以「当前年龄 + 当年」为起点，之后每年 +1。

    人工改过年龄后必须调用本函数重置基准，否则下一次刷新会按旧基准把人工值覆盖掉。
    """
    return {AGE_BASELINE_FIELD: int(age), AGE_BASELINE_YEAR_FIELD: int(year)}


def current_age(parsed_data: dict, year: int) -> int | None:
    """按基准推算当前年龄；没有基准或没有年龄时返回 ``None``（保持原值）。"""
    baseline = _as_int(parsed_data.get(AGE_BASELINE_FIELD))
    baseline_year = _as_int(parsed_data.get(AGE_BASELINE_YEAR_FIELD))
    if baseline is not None and baseline_year is not None:
        return baseline + max(0, year - baseline_year)
    return _as_int(parsed_data.get("age"))


def _batched(items: list[str], size: int):
    for start in range(0, len(items), size):
        yield items[start:start + size]


def _refresh_revision(revision: ResumeRevision, day: date) -> bool:
    """重算一条版本的年滚动字段并就地改写 ``parsed_data``；返回是否有改动。"""
    data = dict(revision.parsed_data or {})
    changed = False

    previous_years = _as_decimal(data.get("total_years"))
    computed_years = compute_total_years(data.get("experiences"), day)
    effective_years = previous_years
    if computed_years is not None and computed_years != previous_years:
        data["total_years"] = str(computed_years)
        effective_years = computed_years
        changed = True

    age = _as_int(data.get("age"))
    baseline = _as_int(data.get(AGE_BASELINE_FIELD))
    baseline_year = _as_int(data.get(AGE_BASELINE_YEAR_FIELD))
    if age is not None and (baseline is None or baseline_year is None):
        # 首次建立基准：年龄当次保持不变，从本年起每年 +1（不补历史欠账）。
        data.update(established_age_baseline(age, day.year))
        changed = True
    elif baseline is not None and baseline_year is not None:
        rolled = baseline + max(0, day.year - baseline_year)
        if rolled != age:
            data["age"] = rolled
            changed = True

    # 画像文本只改「总年限」「年龄」两个数字；其余文字与措辞不动。
    if refresh_profile_fields(
        data, total_years=effective_years, previous_total_years=previous_years
    ):
        changed = True

    if not changed:
        return False
    revision.parsed_data = data
    return True


class WorkYearsRollover:
    """年滚动刷新器（由调度器每日调用一次；重复调用是安全的空操作）。"""

    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        commit_batch_size: int = DEFAULT_COMMIT_BATCH_SIZE,
    ) -> None:
        self.session_factory = session_factory
        self.commit_batch_size = max(1, commit_batch_size)

    def refresh(self, today: date | None = None) -> dict:
        """重算所有当前版本的年滚动字段并同步三处副本。

        返回统计：``scanned`` 扫描的版本数、``updated`` 实际改写的版本数、
        ``candidates`` 受影响的候选人数。
        """
        day = today or datetime.now(SHANGHAI).date()

        # 1. 轻量读：只取标识，不加载 parsed_data，也不开写事务。
        with self.session_factory() as session:
            rows = session.execute(
                select(ResumeRevision.id, ResumeDocument.candidate_id)
                .join(ResumeDocument, ResumeDocument.id == ResumeRevision.document_id)
                .join(Candidate, Candidate.id == ResumeDocument.candidate_id)
                .where(*_refresh_scope())
                # 升序：同一候选人若有多条当前版本，后处理的才是更新的那条。
                .order_by(ResumeRevision.created_at.asc())
            ).all()
        scanned = len(rows)
        candidate_of = {revision_id: candidate_id for revision_id, candidate_id in rows}

        # 2. 分批写入：每批一个独立短事务。
        changed = 0
        latest: dict[str, Decimal | None] = {}
        for batch in _batched([revision_id for revision_id, _ in rows], self.commit_batch_size):
            with self.session_factory() as session, session.begin():
                revisions = list(session.scalars(
                    select(ResumeRevision)
                    .where(ResumeRevision.id.in_(batch))
                    .order_by(ResumeRevision.created_at.asc())
                ).all())
                batch_latest: dict[str, Decimal | None] = {}
                for revision in revisions:
                    if not _refresh_revision(revision, day):
                        continue
                    changed += 1
                    batch_latest[candidate_of[revision.id]] = _as_decimal(
                        (revision.parsed_data or {}).get("total_years")
                    )

                for candidate_id, total_years in batch_latest.items():
                    if total_years is not None:
                        candidate = session.get(Candidate, candidate_id)
                        if candidate is not None:
                            candidate.total_years = total_years
                    # 年限进向量文本与关键词文本、年龄进关键词文本，都要重建索引。
                    enqueue_sync(session, "candidate", candidate_id)

                latest.update(batch_latest)

        if latest:
            logger.info(
                "年滚动刷新：扫描 %d 个版本，更新 %d 个版本 / %d 位候选人",
                scanned, changed, len(latest),
            )
        return {"date": day.isoformat(), "scanned": scanned, "updated": changed,
                "candidates": len(latest)}
