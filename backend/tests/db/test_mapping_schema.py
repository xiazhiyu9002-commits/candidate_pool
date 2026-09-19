from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from kerui_recruit.db.migrate import migrate
from kerui_recruit.db.models import MappingNode, MappingProject, MappingSnapshot
from kerui_recruit.db.session import create_engine_for


def make_session(database: Path) -> Session:
    engine = create_engine_for(database)
    migrate(engine)
    return Session(engine)


def test_mapping_project_snapshot_and_node_tree(tmp_path: Path) -> None:
    with make_session(tmp_path / "recruit.sqlite3") as session:
        project = MappingProject(name="互联网公司图谱")
        session.add(project)
        session.flush()

        snapshot = MappingSnapshot(project_id=project.id, label="v1", is_current=True)
        session.add(snapshot)
        session.flush()

        root = MappingNode(
            snapshot_id=snapshot.id,
            parent_id=None,
            name="字节跳动",
            sort_order=0,
        )
        session.add(root)
        session.flush()

        child = MappingNode(
            snapshot_id=snapshot.id,
            parent_id=root.id,
            name="技术部",
            sort_order=1,
        )
        session.add(child)
        session.commit()

        # Reload and verify relationships
        loaded = session.scalars(select(MappingProject)).one()
        assert loaded.name == "互联网公司图谱"
        assert len(loaded.snapshots) == 1
        snap = loaded.snapshots[0]
        assert snap.label == "v1"
        assert len(snap.nodes) == 2
        roots = [n for n in snap.nodes if n.parent_id is None]
        assert len(roots) == 1
        assert roots[0].children[0].name == "技术部"