from pathlib import Path

import pytest
from sqlalchemy.orm import Session, sessionmaker

from kerui_recruit.db.migrate import migrate
from kerui_recruit.db.session import create_engine_for
from kerui_recruit.mapping.service import MappingService


@pytest.fixture
def session_factory(tmp_path: Path) -> sessionmaker[Session]:
    engine = create_engine_for(tmp_path / "recruit.sqlite3")
    migrate(engine)
    return sessionmaker(engine, expire_on_commit=False)


def test_create_project(session_factory: sessionmaker[Session]) -> None:
    service = MappingService(session_factory=session_factory)
    project = service.create_project(name="互联网公司图谱", description="测试")
    assert project.name == "互联网公司图谱"
    assert project.description == "测试"


def test_build_tree_from_text(session_factory: sessionmaker[Session]) -> None:
    service = MappingService(session_factory=session_factory)
    project = service.create_project(name="互联网公司图谱")

    text = """字节跳动
  技术部
    后端组
    前端组
  产品部
    增长组
腾讯
  微信事业群"""

    snapshot = service.build_tree_from_text(
        project_id=project.id,
        text=text,
        label="初版",
    )
    assert snapshot.label == "初版"

    roots = service.get_tree(snapshot.id)
    assert len(roots) == 2  # 字节跳动, 腾讯

    bytedance = roots[0]
    assert bytedance.name == "字节跳动"
    assert len(bytedance.children) == 2  # 技术部, 产品部

    tech = bytedance.children[0]
    assert tech.name == "技术部"
    assert len(tech.children) == 2  # 后端组, 前端组
    assert tech.children[0].name == "后端组"
    assert tech.children[1].name == "前端组"

    product = bytedance.children[1]
    assert product.name == "产品部"
    assert len(product.children) == 1
    assert product.children[0].name == "增长组"

    tencent = roots[1]
    assert tencent.name == "腾讯"
    assert len(tencent.children) == 1
    assert tencent.children[0].name == "微信事业群"


def test_create_snapshot_and_get_empty_tree(session_factory: sessionmaker[Session]) -> None:
    service = MappingService(session_factory=session_factory)
    project = service.create_project(name="测试项目")
    snapshot = service.create_snapshot(project.id, "空快照")
    assert snapshot.is_current is True

    roots = service.get_tree(snapshot.id)
    assert roots == ()