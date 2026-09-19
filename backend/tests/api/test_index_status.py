from fastapi.testclient import TestClient
from pydantic import SecretStr

from kerui_recruit.core.settings import Settings
from kerui_recruit.db.models import Candidate, IndexSyncRecord, Jd, JdRevision
from kerui_recruit.runtime import create_runtime_app
from kerui_recruit.search.sync import enqueue_sync
from sqlalchemy import select


def test_index_status_and_retry_only_schedule_work(tmp_path):
    app = create_runtime_app(Settings(data_root=tmp_path / 'data', session_token=SecretStr('test')))
    factory = app.state.services.session_factory
    with factory() as session, session.begin():
        candidate = Candidate(display_name='Test')
        session.add(candidate)
        session.flush()
        enqueue_sync(session, 'candidate', candidate.id)
        job = session.scalar(select(IndexSyncRecord))
        job.status, job.attempts, job.last_error = 'RETRY_WAIT', 2, 'TimeoutError'
    client = TestClient(app)
    headers = {'X-Kerui-Session': 'test'}
    before = client.get('/api/search/index-status', headers=headers)
    assert before.status_code == 200
    assert (before.json()['pending'], before.json()['failed']) == (1, 1)
    retried = client.post('/api/search/index-retry', json={}, headers=headers)
    assert retried.status_code == 200
    assert (retried.json()['pending'], retried.json()['failed']) == (1, 0)
    with factory() as session:
        job = session.scalar(select(IndexSyncRecord))
        assert job.applied_version == 0 and job.requested_version == 1
        assert job.next_attempt_at is None
    assert client.get('/api/search/index-status').status_code == 401


def test_pending_jd_matching_is_a_readable_conflict(tmp_path):
    app = create_runtime_app(Settings(data_root=tmp_path / 'data', session_token=SecretStr('test')))
    with app.state.services.session_factory() as session, session.begin():
        jd = Jd(company='Test', title='Engineer', status='OPEN')
        revision = JdRevision(jd=jd, revision_no=1, status='PENDING', is_current=True, source_text='Python')
        session.add(revision)
        session.flush()
        revision_id = revision.id
    response = TestClient(app).post('/api/match/jd', json={'revision_id': revision_id},
        headers={'X-Kerui-Session': 'test'})
    assert response.status_code == 409
    assert response.json()['code'] == 'E_MATCH_NOT_ELIGIBLE'
