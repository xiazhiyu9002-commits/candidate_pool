from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from kerui_recruit.db.models import MappingNode, MappingProject, MappingSnapshot


@dataclass(frozen=True, slots=True)
class TreeNode:
    id: str
    name: str
    sort_order: int
    children: tuple[TreeNode, ...]


class MappingService:
    """Manage mapping projects, snapshots, and tree structures."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self.session_factory = session_factory

    def create_project(self, *, name: str, description: str | None = None) -> MappingProject:
        with self.session_factory() as session:
            project = MappingProject(name=name, description=description)
            session.add(project)
            session.commit()
            return project

    def create_snapshot(self, project_id: str, label: str) -> MappingSnapshot:
        with self.session_factory() as session:
            project = session.get(MappingProject, project_id)
            if project is None:
                raise LookupError(f"MappingProject not found: {project_id}")

            # Mark all existing snapshots as not current
            for snap in project.snapshots:
                snap.is_current = False

            snapshot = MappingSnapshot(project_id=project_id, label=label, is_current=True)
            session.add(snapshot)
            session.commit()
            return snapshot

    def build_tree_from_text(
        self,
        *,
        project_id: str,
        text: str,
        label: str = "",
    ) -> MappingSnapshot:
        """Parse indented text lines into a tree and persist as a new snapshot."""
        snapshot = self.create_snapshot(
            project_id=project_id,
            label=label or datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
        )

        with self.session_factory() as session:
            node_stack: list[MappingNode] = []
            lines = [line for line in text.splitlines() if line.strip()]

            for line_idx, line in enumerate(lines):
                stripped = line.strip()

                # Determine indent depth from leading whitespace
                indent = len(line) - len(line.lstrip())
                depth = indent // 2  # 2 spaces = 1 level

                # Pop until we find the right parent
                while len(node_stack) > depth:
                    node_stack.pop()

                node = MappingNode(
                    snapshot_id=snapshot.id,
                    parent_id=node_stack[-1].id if node_stack else None,
                    name=stripped,
                    sort_order=line_idx,
                )
                session.add(node)
                session.flush()  # populate node.id for potential children
                node_stack.append(node)

            session.commit()
            return snapshot

    def get_tree(self, snapshot_id: str) -> tuple[TreeNode, ...]:
        with self.session_factory() as session:
            nodes = session.scalars(
                select(MappingNode)
                .where(MappingNode.snapshot_id == snapshot_id)
                .order_by(MappingNode.sort_order)
            ).all()

            # Build mutable intermediate: id → list of child ids
            child_ids: dict[str, list[str]] = {}
            for node in nodes:
                child_ids.setdefault(node.parent_id or "__root__", []).append(node.id)

            # Recursively build frozen TreeNode from leaf to root
            def build(parent_id: str) -> tuple[TreeNode, ...]:
                ids = child_ids.get(parent_id, [])
                result = []
                for nid in ids:
                    node = next(n for n in nodes if n.id == nid)
                    result.append(
                        TreeNode(
                            id=node.id,
                            name=node.name,
                            sort_order=node.sort_order,
                            children=build(node.id),
                        )
                    )
                return tuple(result)

            return build("__root__")