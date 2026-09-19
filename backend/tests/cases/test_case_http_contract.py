from fastapi.testclient import TestClient
from pydantic import SecretStr

from kerui_recruit.core.settings import Settings
from kerui_recruit.db.models import Candidate, Jd
from kerui_recruit.runtime import create_runtime_app


def test_http_state_conflict_keeps_existing_case_and_historical_correction_accessible(tmp_path):
    app = create_runtime_app(Settings(data_root=tmp_path / "data", session_token=SecretStr("case-token")))
    headers = {"X-Kerui-Session": "case-token"}
    with TestClient(app) as client:
        services = app.state.services
        with services.session_factory() as session, session.begin():
            candidate = Candidate(display_name="合同测试")
            jd = Jd(company="测试公司", title="开发", status="OPEN")
            session.add_all([candidate, jd])
            session.flush()
            candidate_id, jd_id = candidate.id, jd.id
        created = client.post("/api/case", json={"candidate_id": candidate_id, "jd_id": jd_id}, headers=headers)
        assert created.status_code == 200
        case_id = created.json()["id"]
        entered = client.post(f"/api/case/{case_id}/enter-interview", json={}, headers=headers)
        assert entered.status_code == 200
        round_id = entered.json()["case_round_id"]
        with services.session_factory() as session, session.begin():
            session.get(Candidate, candidate_id).status = "ON_HOLD"
        conflict = client.post(f"/api/case/{case_id}/offer", json={}, headers=headers)
        assert conflict.status_code == 409
        assert conflict.json()["code"] == "E_CASE_STATE_CONFLICT"
        existing = client.post("/api/case", json={"candidate_id": candidate_id, "jd_id": jd_id}, headers=headers)
        assert existing.status_code == 200
        assert existing.json()["id"] == case_id
        assert existing.json()["can_advance"] is False
        corrected = client.post(f"/api/case/{case_id}/result",
            json={"case_round_id": round_id, "result": "通过", "note": "纠正历史反馈"}, headers=headers)
        assert corrected.status_code == 200
        voided = client.post(f"/api/case/event/{corrected.json()['id']}/void",
                            json={"note": "重新核实"}, headers=headers)
        assert voided.status_code == 200
        detail = client.get(f"/api/case/{case_id}", headers=headers)
        assert detail.status_code == 200
        assert detail.json()["can_advance"] is False
        assert detail.json()["events"][0]["occurred_at"].endswith("Z")


def test_http_final_round_rejects_implicit_next_round_and_accepts_final_pass(tmp_path):
    app = create_runtime_app(Settings(data_root=tmp_path / "data", session_token=SecretStr("case-token")))
    headers = {"X-Kerui-Session": "case-token"}
    with TestClient(app) as client:
        services = app.state.services
        with services.session_factory() as session, session.begin():
            candidate = Candidate(display_name="终轮测试")
            jd = Jd(company="测试公司", title="开发", status="OPEN")
            session.add_all([candidate, jd])
            session.flush()
            candidate_id, jd_id = candidate.id, jd.id
        services.case_service.set_process(jd_id, [{"round_no": 1, "round_name": "终面"}])
        created = client.post("/api/case", json={"candidate_id": candidate_id, "jd_id": jd_id}, headers=headers)
        case_id = created.json()["id"]
        initial_detail = client.get(f"/api/case/{case_id}", headers=headers).json()
        assert initial_detail["process_rounds"] == [{"round_no": 1, "round_name": "终面", "round_type": None}]
        assert initial_detail["template_version"] == 1
        assert initial_detail["rounds"] == []
        services.case_service.set_process(jd_id, [{"round_no": 1, "round_name": "新模板首轮"}, {"round_no": 2, "round_name": "新模板终轮"}])
        entered = client.post(f"/api/case/{case_id}/enter-interview", json={}, headers=headers)
        round_id = entered.json()["case_round_id"]
        conflict = client.post(f"/api/case/{case_id}/pass-and-advance", json={"case_round_id": round_id}, headers=headers)
        assert conflict.status_code == 409
        assert conflict.json()["code"] == "E_CASE_STATE_CONFLICT"
        passed = client.post(f"/api/case/{case_id}/result",
                             json={"case_round_id": round_id, "result": "通过"}, headers=headers)
        assert passed.status_code == 200
        detail = client.get(f"/api/case/{case_id}", headers=headers).json()
        assert len(detail["rounds"]) == 1
        assert detail["rounds"][0]["round_name"] == "终面"
        assert detail["process_rounds"] == [{"round_no": 1, "round_name": "终面", "round_type": None}]
        assert detail["template_version"] == 1
        assert [event["result"] for event in detail["events"]] == [None, "通过"]
