from pathlib import Path

import httpx
import pytest

from kerui_recruit.db.models import TaskRecord
from kerui_recruit.main import create_app
from kerui_recruit.resumes.ingest import IngestResume, ResumeIngestService
from tests.api.test_local_api import build_services, make_pdf_bytes


@pytest.mark.asyncio
async def test_reparse_can_be_requested_again_after_success(tmp_path: Path) -> None:
    services, _ = build_services(tmp_path)
    with services.session_factory() as session:
        imported = ResumeIngestService(session, services.blob_store).ingest(
            IngestResume(filename="resume.pdf", content=make_pdf_bytes()))
    transport = httpx.ASGITransport(app=create_app(services))
    async with httpx.AsyncClient(transport=transport, base_url="http://local",
                               headers={"X-Kerui-Session": "test-token"}) as client:
        path = f"/api/resumes/revisions/{imported.revision_id}/reparse"
        first = (await client.post(path, json={"force_ocr": True})).json()
        duplicate = (await client.post(path, json={"force_ocr": True})).json()
        assert duplicate["task_id"] == first["task_id"]
        with services.session_factory() as session, session.begin():
            session.get(TaskRecord, first["task_id"]).status = "SUCCESS"
        second = (await client.post(path, json={"force_ocr": True})).json()
        assert second["task_id"] != first["task_id"]
