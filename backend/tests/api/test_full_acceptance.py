from __future__ import annotations

import time
from pathlib import Path

import pymupdf
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from kerui_recruit.core.settings import Settings
from kerui_recruit.runtime import create_runtime_app

# 脱敏简历样本（无真实个人信息），用于 spec 12.2.4 端到端验收。
_SAMPLE_TEXT = "张三 本科 6年 Java 后端开发 金融风控 上海 Python 支付"


@pytest.fixture
def client(tmp_path: Path):
    settings = Settings(
        data_root=tmp_path / "data",
        session_token=SecretStr("launch-token"),
    )
    # 端到端解析依赖真实 AI provider；离线本地 provider 无法完成结构化解析，
    # 无 key 时跳过（与 test_real_pdf_review.py 的 opt-in 语义一致）。
    if not settings.llm_enabled:
        pytest.skip("set an AI provider key for end-to-end acceptance")
    app = create_runtime_app(settings)
    with TestClient(app) as test_client:
        yield test_client


def _headers() -> dict[str, str]:
    return {"X-Kerui-Session": "launch-token"}


def _pdf_bytes(text: str) -> bytes:
    document = pymupdf.open()
    document.new_page().insert_text((72, 72), text, fontname="china-s")
    payload = document.tobytes()
    document.close()
    return payload


def _wait_task(client: TestClient, task_id: str, timeout: float = 20.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        response = client.get(f"/api/tasks/{task_id}", headers=_headers())
        if response.status_code == 200:
            body = response.json()
            if body["status"] in ("SUCCESS", "FAILED", "DEAD_LETTER"):
                return body
        time.sleep(0.2)
    raise TimeoutError(f"task {task_id} did not finish")


def test_desensitized_sample_end_to_end(client: TestClient) -> None:
    pdf = _pdf_bytes(_SAMPLE_TEXT)

    # 1. 解析：导入简历并等待后台任务解析完成
    imported = client.post(
        "/api/resumes/import",
        files={"file": ("张三.pdf", pdf, "application/pdf")},
        headers=_headers(),
    )
    assert imported.status_code == 202
    first = imported.json()
    assert _wait_task(client, first["task_id"])["status"] == "SUCCESS"

    # 1a. 版本历史：候选人的简历版本可列出
    revisions = client.get(
        f"/api/resumes/candidate/{first['candidate_id']}/revisions",
        headers=_headers(),
    )
    assert revisions.status_code == 200
    assert revisions.json()[0]["is_current"] is True

    # 2. 去重：再次导入同一份原件，返回 ALREADY_IMPORTED 且不新建候选人
    again = client.post(
        "/api/resumes/import",
        files={"file": ("张三.pdf", pdf, "application/pdf")},
        headers=_headers(),
    )
    assert again.status_code == 202
    assert again.json()["action"] == "ALREADY_IMPORTED"
    assert again.json()["candidate_id"] == first["candidate_id"]
    assert again.json()["revision_id"] == first["revision_id"]

    # 3. 搜索：解析后的候选人可被检索
    searched = client.post(
        "/api/search/candidates",
        json={"query": "Java 金融", "filters": {}, "limit": 20},
        headers=_headers(),
    )
    assert searched.status_code == 200
    assert any(
        item["candidate_id"] == first["candidate_id"]
        for item in searched.json()["items"]
    )

    # 4. 匹配：导入 JD 并匹配
    jd = client.post(
        "/api/jd/import",
        json={"company": "某金融", "title": "Java 后端", "source_text": "Java 3年 本科 金融"},
        headers=_headers(),
    )
    assert jd.status_code == 202
    # Import is asynchronous; a pending parse is deliberately not matchable.
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        jobs = client.get("/api/jd", headers=_headers()).json()
        current = next(item for item in jobs if item["revision_id"] == jd.json()["revision_id"])
        if current["status"] == "READY":
            assert current["jd_status"] == "OPEN"
            pending = client.app.state.services.index_sync_service.status()["items"]
            if not any(item["entity_type"] == "jd" and item["entity_id"] == current["jd_id"] for item in pending):
                break
        time.sleep(.05)
    else:
        raise AssertionError("Local JD parsing did not finish")
    matched = client.post(
        "/api/match/jd",
        json={"revision_id": jd.json()["revision_id"], "limit": 20},
        headers=_headers(),
    )
    assert matched.status_code == 200
    run_id = matched.json()["run_id"]
    assert run_id

    # 4a. 收藏/人工标记：对匹配结果打标记
    items = matched.json()["items"]
    assert any(item["candidate_id"] == first["candidate_id"] for item in items)
    result_id = next(item["result_id"] for item in items if item["candidate_id"] == first["candidate_id"])
    marked = client.post(
        f"/api/match/result/{result_id}/mark",
        json={"status": "保留"},
        headers=_headers(),
    )
    assert marked.status_code == 200
    assert marked.json()["status"] == "保留"

    # 5. 导出：匹配结果可导出为 Excel
    exported = client.get(f"/api/match/run/{run_id}/export", headers=_headers())
    assert exported.status_code == 200
    assert exported.headers["content-type"].startswith(
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
