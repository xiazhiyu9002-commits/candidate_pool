from __future__ import annotations

import io

from openpyxl import Workbook
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from kerui_recruit.db.models import Candidate, MappingNode, MatchResult, MatchRun


class ExportService:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self.session_factory = session_factory

    def export_match_run(self, run_id: str) -> bytes:
        with self.session_factory() as session:
            run = session.get(MatchRun, run_id)
            if run is None:
                raise LookupError(f"MatchRun not found: {run_id}")
            results = session.scalars(
                select(MatchResult).where(MatchResult.run_id == run_id)
            ).all()
            return self._match_workbook(session, results)

    def export_match_jd(self, revision_id: str) -> bytes:
        """Export every persisted match result for a single JD revision."""
        with self.session_factory() as session:
            results = session.scalars(
                select(MatchResult)
                .where(MatchResult.jd_revision_id == revision_id)
                .order_by(MatchResult.total_score.desc())
            ).all()
            return self._match_workbook(session, results)

    def _match_workbook(self, session: Session, results) -> bytes:
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "匹配结果"
        sheet.append(["候选人", "总分", "处理状态", "推荐理由"])

        for result in results:
            candidate = session.get(Candidate, result.candidate_id)
            name = candidate.display_name if candidate else result.candidate_id
            sheet.append(
                [name, float(result.total_score or 0), result.status, result.reason or ""]
            )

        buffer = io.BytesIO()
        workbook.save(buffer)
        return buffer.getvalue()

    def export_mapping_tree(self, snapshot_id: str) -> bytes:
        """Flatten an org-tree snapshot into an Excel sheet."""
        with self.session_factory() as session:
            nodes = session.scalars(
                select(MappingNode)
                .where(MappingNode.snapshot_id == snapshot_id)
                .order_by(MappingNode.sort_order)
            ).all()

        if not nodes:
            raise LookupError(f"Mapping snapshot not found or empty: {snapshot_id}")

        node_map = {node.id: node for node in nodes}

        def depth(node: MappingNode) -> int:
            level = 0
            current = node
            while current.parent_id and current.parent_id in node_map:
                level += 1
                current = node_map[current.parent_id]
            return level

        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "组织架构"
        sheet.append(["层级", "节点名称", "父节点"])

        for node in nodes:
            parent = node_map[node.parent_id].name if node.parent_id else ""
            sheet.append([depth(node), node.name, parent])

        buffer = io.BytesIO()
        workbook.save(buffer)
        return buffer.getvalue()

    def export_mapping_tree_pdf(self, snapshot_id: str) -> bytes:
        """Render an org-tree snapshot as an indented, auto-paginated PDF."""
        import html as html_module

        import pymupdf

        with self.session_factory() as session:
            nodes = session.scalars(
                select(MappingNode)
                .where(MappingNode.snapshot_id == snapshot_id)
                .order_by(MappingNode.sort_order)
            ).all()

        if not nodes:
            raise LookupError(f"Mapping snapshot not found or empty: {snapshot_id}")

        node_map = {node.id: node for node in nodes}

        def depth(node: MappingNode) -> int:
            level = 0
            current = node
            while current.parent_id and current.parent_id in node_map:
                level += 1
                current = node_map[current.parent_id]
            return level

        # 每个节点一个段落，按层级缩进；内容做 HTML 转义，避免特殊字符破坏排版。
        paragraphs = [
            f'<p style="margin:0 0 4px 0; text-indent:{depth(node) * 24}px;">'
            f'{html_module.escape(node.name or "")}</p>'
            for node in nodes
        ]
        body = "".join(paragraphs)

        import gc
        import os
        import shutil
        import tempfile

        tmpdir = tempfile.mkdtemp()
        pdf_path = os.path.join(tmpdir, "mapping.pdf")
        try:
            writer = pymupdf.DocumentWriter(pdf_path)
            story = pymupdf.Story(
                html=body,
                user_css='body { font-family: "china-s", sans-serif; font-size: 11px; }',
            )
            mediabox = pymupdf.paper_rect("a4")
            where = pymupdf.Rect(72, 72, mediabox.width - 72, mediabox.height - 72)

            # Story 自动换行 + 自动分页，避免长内容互相遮挡。
            more = 1
            device = None
            while more:
                device = writer.begin_page(mediabox)
                more, _ = story.place(where)
                story.draw(device)
                writer.end_page()

            writer.close()
            # Windows 下显式释放底层文件句柄，确保临时 PDF 可被清理。
            del device, writer, story
            gc.collect()
            with open(pdf_path, "rb") as fh:
                return fh.read()
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)