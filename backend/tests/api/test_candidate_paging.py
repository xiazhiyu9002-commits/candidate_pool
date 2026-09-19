from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from kerui_recruit.core.settings import Settings
from kerui_recruit.db.models import Blob, Candidate, ResumeDocument, ResumeRevision
from kerui_recruit.main import create_app
from kerui_recruit.runtime import build_runtime


def test_candidate_page_is_bounded_and_reports_total(tmp_path):
    runtime = build_runtime(Settings(data_root=tmp_path / "data", session_token="test"))
    with runtime.services.session_factory() as session, session.begin():
        blob = Blob(content_sha256="f" * 64, suffix=".pdf", size_bytes=1, storage_path="x")
        session.add(blob)
        for number in range(3):
            candidate = Candidate(display_name=f"Candidate {number}")
            session.add(ResumeRevision(
                document=ResumeDocument(candidate=candidate), blob=blob,
                content_sha256=f"{number:064x}", original_filename=f"{number}.pdf",
                status="READY", is_current=True, parsed_data={"skills": ["Python"]},
            ))
    with TestClient(create_app(runtime.services)) as client:
        response = client.get("/api/resumes/candidates/page?page=2&page_size=2",
                              headers={"X-Kerui-Session": "test"})
    assert response.status_code == 200
    assert response.json()["total"] == 3
    assert response.json()["page"] == 2
    assert response.json()["page_size"] == 2
    assert response.json()["has_more"] is False
    assert len(response.json()["items"]) == 1
    runtime.services.backup_service.engine.dispose()
