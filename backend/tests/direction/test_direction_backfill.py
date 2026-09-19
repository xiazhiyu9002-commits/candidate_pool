"""历史方向复判测试：跳过人工修订与已确定方向，null/OTHER 复判，dry-run 幂等。"""
from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from kerui_recruit.db.migrate import migrate
from kerui_recruit.db.models import Blob, Candidate, Jd, JdRevision, ResumeDocument, ResumeRevision
from kerui_recruit.db.session import create_engine_for
from kerui_recruit.direction.backfill import audit_all_non_manual, backfill_directions
from kerui_recruit.direction.policy import SPECIALIZATION_PARENT


@pytest.fixture
def factory(tmp_path: Path):
    engine = create_engine_for(tmp_path / "db.sqlite3")
    migrate(engine)
    return sessionmaker(engine, expire_on_commit=False)


def _seed(factory, revision_id: str, parsed: dict, overrides: dict | None = None) -> None:
    with factory() as session, session.begin():
        sha = revision_id.ljust(64, "x")[:64]
        candidate = Candidate(display_name="测试", status="AVAILABLE")
        blob = Blob(content_sha256=sha, suffix=".pdf", size_bytes=1, storage_path=f"unused-{revision_id}")
        document = ResumeDocument(candidate=candidate)
        revision = ResumeRevision(
            id=revision_id,
            document=document, blob=blob, content_sha256=sha,
            original_filename=f"{revision_id}.pdf", status="READY", is_current=True,
            parsed_data=parsed, manual_overrides=overrides,
        )
        session.add(revision)
        session.flush()


def _direction(factory, revision_id: str) -> object:
    with factory() as session:
        return session.get(ResumeRevision, revision_id).parsed_data.get("direction")


def test_null_direction_is_reclassified(factory):
    _seed(factory, "r1", {"skills": ["Flink"], "summary": "数据管道开发", "direction": None})
    result = backfill_directions(factory, dry_run=False)
    assert result["pending"] == 1
    assert result["changed"] == 1
    assert _direction(factory, "r1") == "BACKEND"


def test_other_direction_is_reclassified(factory):
    _seed(factory, "r2", {"skills": ["PyTorch"], "summary": "模型训练", "direction": "OTHER"})
    backfill_directions(factory, dry_run=False)
    assert _direction(factory, "r2") == "ALGORITHM"


def test_legacy_single_direction_is_upgraded_not_skipped(factory):
    """存量只有旧单值 direction：必须升级为多值体系，否则新字段与硬门槛在存量上失效。"""
    _seed(factory, "r3", {"skills": ["Java", "Spring"], "summary": "后端微服务开发",
                          "direction": "BACKEND"})
    result = backfill_directions(factory, dry_run=False)
    assert result["pending"] == 1
    assert result["changed"] == 1
    data = _parsed(factory, "r3")
    # 旧方向作为锚点保留，不被关键词复判覆盖
    assert data["direction"] == "BACKEND"
    assert data["career_directions"][0] == "BACKEND"
    assert data["career_taxonomy_version"] == "4"


def test_manual_direction_override_is_not_overwritten(factory):
    _seed(factory, "r4", {"skills": ["Java"], "summary": "后端", "direction": None}, overrides={"direction": "ALGORITHM"})
    backfill_directions(factory, dry_run=False)
    assert _direction(factory, "r4") is None  # 人工修订优先，回填不覆盖


def test_dry_run_does_not_write(factory):
    _seed(factory, "r5", {"skills": ["Java"], "summary": "后端", "direction": None})
    result = backfill_directions(factory, dry_run=True)
    assert result["pending"] == 1
    assert result["changed"] == 0
    assert _direction(factory, "r5") is None


def test_rerun_is_idempotent(factory):
    _seed(factory, "r6", {"skills": ["Java"], "summary": "后端", "direction": None})
    first = backfill_directions(factory, dry_run=False)
    second = backfill_directions(factory, dry_run=False)
    assert first["changed"] == 1
    assert second["changed"] == 0  # 复判后已确认，重跑不重复
    assert _direction(factory, "r6") == "BACKEND"


def _seed_jd(factory, revision_id: str, parsed: dict, overrides: dict | None = None) -> None:
    with factory() as session, session.begin():
        jd = Jd(company="测试公司", title="测试岗位", status="OPEN")
        revision = JdRevision(
            id=revision_id, jd=jd, revision_no=1, status="READY", is_current=True,
            source_text="测试", parsed_data=parsed, manual_overrides=overrides,
        )
        session.add(revision)
        session.flush()


def _jd_direction(factory, revision_id: str) -> object:
    with factory() as session:
        return session.get(JdRevision, revision_id).parsed_data.get("direction")


def test_jd_null_direction_is_reclassified(factory):
    _seed_jd(factory, "jd1", {"skills": ["Flink"], "summary": "数据管道开发", "direction": None})
    result = backfill_directions(factory, dry_run=False, entity_type="jd")
    assert result["pending"] == 1
    assert result["changed"] == 1
    assert _jd_direction(factory, "jd1") == "BACKEND"


def test_jd_manual_direction_override_is_not_overwritten(factory):
    _seed_jd(factory, "jd2", {"skills": ["Java"], "summary": "后端", "direction": None}, overrides={"direction": "ALGORITHM"})
    backfill_directions(factory, dry_run=False, entity_type="jd")
    assert _jd_direction(factory, "jd2") is None  # 人工修订优先


def test_audit_all_non_manual_reports_change_conflict_manual(factory):
    # null 待核 -> 复判 BACKEND；合法 DATA -> 复判 BACKEND（冲突）；人工覆盖跳过。
    _seed(factory, "a1", {"skills": ["Flink"], "summary": "数据管道开发", "direction": None})
    _seed(factory, "a2", {"skills": ["Flink"], "summary": "数据平台 API 开发", "direction": "DATA"})
    _seed(factory, "a3", {"skills": ["SQL"], "summary": "数仓", "direction": "DATA"}, overrides={"direction": "DATA"})
    result = audit_all_non_manual(factory)
    assert result["scanned"] == 3
    assert result["manual"] == 1
    assert result["would_change"] == 2
    assert result["conflict"] == 1  # 合法枚举但复判不同（DATA -> BACKEND）


def _parsed(factory, revision_id: str) -> dict:
    with factory() as session:
        return dict(session.get(ResumeRevision, revision_id).parsed_data or {})


def test_backfill_writes_multi_value_direction_fields(factory):
    """回填必须一次写全多值方向字段，否则索引与匹配拿不到新列。"""
    _seed(factory, "m1", {
        "skills": ["Java", "Spring", "Flink"],
        "summary": "负责保险营销平台的微服务与数据管道开发",
        "direction": None,
        "experiences": [
            {"summary": "保险营销平台微服务开发", "industry": "保险", "start_date": "2020.01", "end_date": "至今"},
        ],
        "projects": [{"name": "营销活动系统", "business_scene": "保险营销投放", "summary": "营销活动配置"}],
    })
    backfill_directions(factory, dry_run=False)
    data = _parsed(factory, "m1")

    assert data["direction"] == "BACKEND"
    assert data["career_directions"][0] == "BACKEND"
    assert len(data["career_directions"]) <= 2
    assert len(data["career_specializations"]) <= 4
    # 细分必须归属已选大类（不再限定只能是 BACKEND_*：数据管道证据会带出 DATA_*）
    assert data["career_specializations"], "应产出细分"
    assert all(
        SPECIALIZATION_PARENT[code] in data["career_directions"]
        for code in data["career_specializations"]
    )
    assert data["business_directions"], "应有业务方向"
    assert set(data["business_directions"]) <= {"INSURANCE", "MARKETING"}
    assert data["career_taxonomy_version"] == "4"


def test_backfill_syncs_direction_assessment_when_present(factory):
    _seed(factory, "m2", {
        "skills": ["Java", "Spring"],
        "summary": "后端微服务",
        "direction": None,
        "direction_assessment": {"primary": None, "confidence": "low"},
    })
    backfill_directions(factory, dry_run=False)
    assessment = _parsed(factory, "m2")["direction_assessment"]
    assert assessment["career_directions"]
    assert assessment["specializations"] is not None
    assert assessment["business_directions"] is not None


def test_backfill_does_not_override_new_multi_value_directions(factory):
    """已有新字段方向时不再复判（幂等，且保护人工/新解析结果）。"""
    _seed(factory, "m3", {
        "skills": ["PyTorch"], "summary": "模型训练",
        "career_directions": ["ALGORITHM"], "career_specializations": ["ALGORITHM_TRAINING"],
    })
    result = backfill_directions(factory, dry_run=False)
    assert result["pending"] == 0
    assert _parsed(factory, "m3")["career_directions"] == ["ALGORITHM"]


def test_backfill_skips_manual_multi_value_override(factory):
    _seed(factory, "m4", {"skills": ["Java"], "summary": "后端", "direction": None},
          overrides={"career_specializations": ["BACKEND_SERVICE"]})
    backfill_directions(factory, dry_run=False)
    assert _direction(factory, "m4") is None  # 人工修订优先，回填不覆盖


def test_existing_direction_stays_first_when_classifier_adds_two_more(factory):
    """回归：分类器给出 2 个大类时不得把已有方向挤到第 2 位。

    真实数据上出现过：旧方向 BACKEND + 分类器 (FRONTEND, OPS) → 结果变成
    ['FRONTEND', 'BACKEND']，单值 direction 镜像也被改写。
    """
    _seed(factory, "e1", {
        "skills": ["React", "TypeScript"],
        "summary": "前端页面与小程序开发",
        "direction": "BACKEND",
        "experiences": [{"summary": "前端页面开发与小程序交付"}],
        "projects": [{"summary": "K8s 集群运维与监控告警"}],
    })
    backfill_directions(factory, dry_run=False)
    data = _parsed(factory, "e1")
    assert data["career_directions"][0] == "BACKEND"
    assert data["direction"] == "BACKEND"


def test_second_slot_prefers_direction_with_specialization_support(factory):
    """第 2 位优先给有细分支撑的大类，避免细分被整体丢弃。"""
    _seed(factory, "e2", {
        "skills": ["Java", "Spring", "Flink", "Kafka"],
        "summary": "数据管道与采集平台开发",
        "direction": "DATA",
        "experiences": [{"summary": "数据管道与数据采集平台开发"}],
        "projects": [{"summary": "实时数据管道建设"}],
    })
    backfill_directions(factory, dry_run=False)
    data = _parsed(factory, "e2")
    assert data["career_directions"][0] == "DATA"
    # 分类器的细分属于 BACKEND（数据平台开发），必须有对应大类承载才能保留
    assert data["career_specializations"], "细分不应因锚点而整体丢失"
    assert all(
        code.startswith("DATA_") or "BACKEND" in data["career_directions"]
        for code in data["career_specializations"]
    )
