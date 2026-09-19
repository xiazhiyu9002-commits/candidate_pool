"""索引版本升级编排：把「装上新版后必须手动重建索引」变成启动时自动完成。

分岔依据只有物理共存能力：

- 就地修复（``inplace``）：embedding 模型与向量长度都没变，只是 schema / chunk 版本
  或分块方式变了。补上缺失的可空列并刷新 metadata 即可写，旧行继续可读，全量重新
  投影后新旧数据口径收敛。
- 重置重建（``reset``）：模型或向量长度变了，旧行既读不出也写不回，只能归档旧索引、
  从空索引重新投影；新索引边填边可搜，完成后删除归档。

两条路径都不需要人工介入，也不再保留旧索引作为「可用基线」——发布新版的前提就是
新版向量口径更优，旧索引没有继续沿用的价值。
"""
from __future__ import annotations

import json
import logging
import os
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from kerui_recruit.search.lancedb_index import INDEX_CHUNK_VERSION, INDEX_SCHEMA_VERSION

logger = logging.getLogger(__name__)

REBUILD_STATE_FILE = "index-rebuild.json"

INPLACE = "inplace"
RESET = "reset"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(slots=True)
class IndexUpgradePlan:
    """一次自动重建的执行计划。"""

    mode: str
    reason: str
    archived: Path | None = None

    @property
    def resets(self) -> bool:
        return self.mode == RESET


def plan_index_upgrade(search: Path, *, embedding_model: str,
                       vector_dimension: int) -> IndexUpgradePlan | None:
    """判定该就地修复还是重置重建；两者都不需要时返回 None。

    只看 metadata 文件、不打开 LanceDB：归档是目录改名，必须在任何数据库连接之前
    完成，否则 Windows 上会因句柄占用而改名失败。schema/chunk 版本本来就是「物理
    字段或分块方式改变」的契约声明，因此 metadata 比对足以覆盖全部版本提升。
    """
    expected = {
        "schema_version": INDEX_SCHEMA_VERSION,
        "embedding_model": embedding_model,
        "vector_dimension": vector_dimension,
        "chunk_version": INDEX_CHUNK_VERSION,
    }
    reset_reason: str | None = None
    inplace_reason: str | None = None
    for label, root in (("candidate", search), ("jd", search / "jobs")):
        path = root / "candidate-index-metadata.json"
        if not path.is_file():
            continue  # 空索引：首次写入自建，无需处理
        try:
            # utf-8-sig：外部工具写的 BOM 不该让「版本落后」被误判成索引损坏。
            actual = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            reset_reason = reset_reason or f"{label}: 索引 metadata 缺失或损坏"
            continue
        if actual == expected:
            continue
        if actual.get("embedding_model") != embedding_model:
            reset_reason = reset_reason or f"{label}: embedding 模型已更换"
        elif actual.get("vector_dimension") != vector_dimension:
            reset_reason = reset_reason or f"{label}: 向量维度已更换"
        else:
            inplace_reason = inplace_reason or f"{label}: schema/chunk 版本提升"
    if reset_reason is not None:
        return IndexUpgradePlan(RESET, reset_reason)
    if inplace_reason is not None:
        return IndexUpgradePlan(INPLACE, inplace_reason)
    return None


def archive_search_index(search: Path) -> Path | None:
    """把旧索引归档为同级带时间戳的目录；只改名不复制数据，因此瞬时完成。"""
    if not search.exists() or not any(search.iterdir()):
        return None
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    archived = search.with_name(f"{search.name}.pre-rebuild-{stamp}")
    os.replace(search, archived)
    search.mkdir(parents=True, exist_ok=True)
    return archived


class IndexRebuildTracker:
    """记录一次自动重建的进度（供界面提示），并在队列排空后清理归档的旧索引。

    状态落在数据根目录下的独立文件里，所以中途退出应用也能在下次启动时收尾：
    重建未完成时 metadata 已经是对齐的，不会再次触发重建，但队列里仍有待投影的
    实体，跟踪器据此继续向界面报告进度并完成归档清理。
    """

    def __init__(self, state_path: Path) -> None:
        self._state_path = state_path
        self._cache: dict | None = None
        self._loaded = False

    def begin(self, *, mode: str, reason: str, total: int, archive: Path | None = None) -> None:
        payload = {
            "mode": mode,
            "reason": reason,
            "total": total,
            "started_at": _now(),
            "finished_at": None,
            "archived": str(archive) if archive is not None else None,
        }
        self._write(payload)

    def snapshot(self) -> dict | None:
        if not self._loaded:
            self._loaded = True
            self._cache = self._read()
        return self._cache

    @property
    def active(self) -> bool:
        state = self.snapshot()
        return state is not None and not state.get("finished_at")

    def poll(self, *, pending: int) -> None:
        """队列排空即视为重建完成：落完成时间并删除归档的旧索引。"""
        if pending or not self.active:
            return
        state = dict(self._cache or {})
        state["finished_at"] = _now()
        self._write(state)
        self._discard_archive(state.get("archived"))

    def _discard_archive(self, archived: str | None) -> None:
        if not archived:
            return
        path = Path(archived)
        if not path.exists():
            return
        try:
            shutil.rmtree(path)
        except OSError as error:  # 清理失败不该影响同步循环
            logger.warning("旧索引归档清理失败：%s", error)

    def _read(self) -> dict | None:
        try:
            payload = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return payload if isinstance(payload, dict) else None

    def _write(self, payload: dict) -> None:
        self._cache = payload
        self._loaded = True
        try:
            temporary = self._state_path.with_suffix(".tmp")
            temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            temporary.replace(self._state_path)
        except OSError as error:  # 状态文件写不了不该阻断重建本身
            logger.warning("重建状态写入失败：%s", error)
