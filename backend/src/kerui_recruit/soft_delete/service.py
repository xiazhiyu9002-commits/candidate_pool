"""通用软删除服务：置/清 deleted_at、查询回收站与过期物理清理。

仅支持 Candidate 与 Jd 两类实体（二者均有 deleted_at 字段）。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from kerui_recruit.db.models import Candidate, Jd
from kerui_recruit.search.sync import enqueue_sync

_ENTITY_MODELS = {
    "candidate": Candidate,
    "jd": Jd,
}


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class SoftDeleteService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        candidate_deletion=None,
        jd_deletion=None,
    ) -> None:
        self.session_factory = session_factory
        # 过期物理清理的委托：传了就在清理时复用单条删除服务，
        # 使「先写流程快照、再解除关联」与手动删除完全一致（不丢流程可读性）。
        self.candidate_deletion = candidate_deletion
        self.jd_deletion = jd_deletion

    def soft_delete(self, entity_type: str, entity_id: str) -> bool:
        model = _ENTITY_MODELS.get(entity_type)
        if model is None:
            raise ValueError(f"Unsupported entity type: {entity_type}")
        with self.session_factory() as session, session.begin():
            entity = session.get(model, entity_id)
            if entity is None:
                return False
            entity.deleted_at = _now()
            enqueue_sync(session, entity_type, entity_id)
            if entity_type == "jd":
                from kerui_recruit.cases.state import refresh_links
                refresh_links(session, jd_id=entity_id)
            return True

    def restore(self, entity_type: str, entity_id: str) -> bool:
        model = _ENTITY_MODELS.get(entity_type)
        if model is None:
            raise ValueError(f"Unsupported entity type: {entity_type}")
        with self.session_factory() as session, session.begin():
            entity = session.get(model, entity_id)
            if entity is None:
                return False
            entity.deleted_at = None
            enqueue_sync(session, entity_type, entity_id)
            if entity_type == "jd":
                from kerui_recruit.cases.state import refresh_links
                refresh_links(session, jd_id=entity_id)
            return True

    def is_deleted(self, entity_type: str, entity_id: str) -> bool:
        model = _ENTITY_MODELS.get(entity_type)
        if model is None:
            raise ValueError(f"Unsupported entity type: {entity_type}")
        with self.session_factory() as session:
            entity = session.get(model, entity_id)
            return bool(entity is not None and entity.deleted_at is not None)

    def list_deleted(self) -> list[dict]:
        with self.session_factory() as session:
            candidates = session.scalars(
                select(Candidate).where(Candidate.deleted_at.is_not(None))
            ).all()
            jds = session.scalars(select(Jd).where(Jd.deleted_at.is_not(None))).all()
        items: list[dict] = []
        for c in candidates:
            items.append({
                "entity_type": "candidate",
                "entity_id": c.id,
                "label": c.display_name or c.id,
                "deleted_at": c.deleted_at,
            })
        for j in jds:
            items.append({
                "entity_type": "jd",
                "entity_id": j.id,
                "label": f"{j.company or ''} - {j.title or ''}".strip(" -"),
                "deleted_at": j.deleted_at,
            })
        return items

    def purge_expired(self, retention_days: int = 30) -> int:
        """回收站过期物理清理。

        已配置删除服务时**委托**给它们执行，保证流程快照与解除关联的行为与手动删除一致；
        未配置时退回直接删除（不写快照），仅在无依赖的测试/脚本场景下出现。
        """
        cutoff = _now() - timedelta(days=retention_days)
        with self.session_factory() as session:
            candidate_ids = list(session.scalars(
                select(Candidate.id).where(
                    Candidate.deleted_at.is_not(None), Candidate.deleted_at < cutoff)
            ))
            jd_ids = list(session.scalars(
                select(Jd.id).where(Jd.deleted_at.is_not(None), Jd.deleted_at < cutoff)
            ))

        if self.candidate_deletion is None and self.jd_deletion is None:
            removed = 0
            with self.session_factory() as session, session.begin():
                for entity in session.scalars(
                    select(Candidate).where(
                        Candidate.deleted_at.is_not(None), Candidate.deleted_at < cutoff)
                ).all():
                    session.delete(entity)
                    removed += 1
                for entity in session.scalars(
                    select(Jd).where(Jd.deleted_at.is_not(None), Jd.deleted_at < cutoff)
                ).all():
                    session.delete(entity)
                    removed += 1
            return removed

        removed = 0
        for candidate_id in candidate_ids:
            if self.candidate_deletion is not None and self.candidate_deletion.delete(candidate_id):
                removed += 1
        for jd_id in jd_ids:
            if self.jd_deletion is not None and self.jd_deletion.delete(jd_id):
                removed += 1
        return removed
