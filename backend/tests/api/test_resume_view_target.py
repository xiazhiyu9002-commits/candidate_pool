from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from kerui_recruit.main import create_app
from kerui_recruit.resumes.ingest import IngestResume, ResumeIngestService
from test_local_api import build_services


@pytest.mark.parametrize('filename', ['张三 简历.doc', '张三 简历.DOCX', '../../unsafe:name.docx'])
def test_word_view_target_is_editable_copy_preserving_original(tmp_path, filename):
    services, _ = build_services(tmp_path)
    with services.session_factory() as session:
        result = ResumeIngestService(session, services.blob_store).ingest(IngestResume(filename=filename, content=b'word bytes'))
    with TestClient(create_app(services)) as client:
        response = client.get(f'/api/resumes/revisions/{result.revision_id}/view-target', headers={'X-Kerui-Session': 'test-token'})
        assert response.status_code == 200
        body = response.json()
        assert body['kind'] == 'word'
        assert body['filename'] == filename
        target = Path(body['path'])
        assert target.resolve().is_relative_to((services.settings.paths.temp / 'open-documents').resolve())
        assert target.suffix.lower() == Path(filename).suffix.lower()
        assert ':' not in target.name
        assert target.read_bytes() == b'word bytes'
        target.write_bytes(b'edited')
        original = client.get(f'/api/resumes/revisions/{result.revision_id}/download', headers={'X-Kerui-Session': 'test-token'})
        assert original.content == b'word bytes'
        second = client.get(f'/api/resumes/revisions/{result.revision_id}/view-target', headers={'X-Kerui-Session': 'test-token'}).json()
        assert Path(second['path']).read_bytes() == b'word bytes'


def test_pdf_view_target_and_missing_revision(tmp_path):
    services, _ = build_services(tmp_path)
    with services.session_factory() as session:
        result = ResumeIngestService(session, services.blob_store).ingest(IngestResume(filename='resume.pdf', content=b'%PDF fake'))
    with TestClient(create_app(services)) as client:
        headers = {'X-Kerui-Session': 'test-token'}
        response = client.get(f'/api/resumes/revisions/{result.revision_id}/view-target', headers=headers)
        assert response.status_code == 200
        assert response.json() == {'kind': 'preview', 'filename': 'resume.pdf'}
        assert client.get('/api/resumes/revisions/missing/view-target', headers=headers).status_code == 404
