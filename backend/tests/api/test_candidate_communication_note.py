from __future__ import annotations

from pathlib import Path

import httpx
import pymupdf
import pytest

from kerui_recruit.db.models import Candidate, ResumeRevision
from kerui_recruit.main import create_app
from tests.api.test_local_api import build_services, make_pdf_bytes

HEADERS = {"X-Kerui-Session": "test-token"}
# 故意取一段不会出现在简历原文里的主观判断，用来验证它既没进画像也没进索引。
NOTE = "性格偏内向，沟通需要多推一把；对加班有顾虑。"


async def _import_candidate(tmp_path: Path):
    """导入一份合成简历并跑完解析+索引，返回 (services, client, candidate_id, revision_id)。

    返回的 client 已经发过请求（httpx 会惰性打开），因此调用方**不要**再用
    `async with` 包它；用完 `await client.aclose()` 即可。
    """
    services, pipeline = build_services(tmp_path)
    transport = httpx.ASGITransport(app=create_app(services))
    client = httpx.AsyncClient(transport=transport, base_url="http://local")
    imported = await client.post(
        "/api/resumes/import",
        files={"file": ("张三.pdf", make_pdf_bytes(), "application/pdf")},
        headers=HEADERS,
    )
    assert imported.status_code == 202
    payload = imported.json()
    await pipeline.run(payload["revision_id"])
    assert await services.index_sync_service.run_once(force=True) == 1
    return services, client, payload["candidate_id"], payload["revision_id"]


async def _save_note(client: httpx.AsyncClient, candidate_id: str, note: str) -> httpx.Response:
    return await client.put(
        f"/api/resumes/candidate/{candidate_id}/communication-note",
        json={"note": note},
        headers=HEADERS,
    )


def _pdf_bytes(text: str) -> bytes:
    """生成指定正文的合成 PDF。正文不同 → 内容 sha256 不同，导入时不会被去重合并。"""
    pdf = pymupdf.open()
    pdf.new_page().insert_text((72, 72), text)
    content = pdf.tobytes()
    pdf.close()
    return content


async def _import_two_candidates(tmp_path: Path):
    """导入两份简历得到两个独立候选人，返回 (services, client, [candidate_id, ...])。

    FixedResumeParser 对任何输入都返回同一套解析字段，所以两人的画像/正文关键词一样；
    这里靠简历正文不同（sha256 不同）保证它们是两个候选人，用来验证「命中的人出现、
    没命中的人不出现」。
    """
    services, pipeline = build_services(tmp_path)
    transport = httpx.ASGITransport(app=create_app(services))
    client = httpx.AsyncClient(transport=transport, base_url="http://local")
    candidate_ids: list[str] = []
    for filename, text in (("张三.pdf", "Python Finance Resume A"),
                           ("李四.pdf", "Python Finance Resume B")):
        imported = await client.post(
            "/api/resumes/import",
            files={"file": (filename, _pdf_bytes(text), "application/pdf")},
            headers=HEADERS,
        )
        assert imported.status_code == 202
        payload = imported.json()
        await pipeline.run(payload["revision_id"])
        candidate_ids.append(payload["candidate_id"])
    assert await services.index_sync_service.run_once(force=True) == 2
    return services, client, candidate_ids


async def _search_note(client: httpx.AsyncClient, note: str) -> httpx.Response:
    return await client.post(
        "/api/search/candidates",
        json={"query": "Python", "filters": {"communication_note": note}, "limit": 20},
        headers=HEADERS,
    )


@pytest.mark.asyncio
async def test_note_is_saved_and_returned_in_list_and_search(tmp_path: Path) -> None:
    _services, client, candidate_id, _revision_id = await _import_candidate(tmp_path)
    try:
        saved = await _save_note(client, candidate_id, NOTE)
        assert saved.status_code == 200
        assert saved.json()["communication_note"] == NOTE

        listed = await client.get("/api/resumes/candidates", headers=HEADERS)
        assert listed.status_code == 200
        assert listed.json()[0]["communication_note"] == NOTE

        searched = await client.post(
            "/api/search/candidates",
            json={"query": "Python", "filters": {}, "limit": 20},
            headers=HEADERS,
        )
        assert searched.status_code == 200
        assert searched.json()["items"][0]["communication_note"] == NOTE
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_note_does_not_touch_profile_state(tmp_path: Path) -> None:
    """沟通记录必须与画像状态完全隔离。

    这类偏软性的主观判断一旦写进 `parsed_data`，就会顺着画像污染向量与检索结果，
    所以这里直接断言 `parsed_data` 与画像状态一字未变。
    """
    services, client, candidate_id, revision_id = await _import_candidate(tmp_path)
    with services.session_factory() as session:
        before = dict(session.get(ResumeRevision, revision_id).parsed_data or {})
        assert session.get(Candidate, candidate_id).communication_note is None

    try:
        assert (await _save_note(client, candidate_id, NOTE)).status_code == 200
    finally:
        await client.aclose()

    with services.session_factory() as session:
        after = dict(session.get(ResumeRevision, revision_id).parsed_data or {})
        assert after == before
        # 画像过期标记与输入哈希都不能被这条备注搅动，否则会触发不必要的重新生成。
        assert after.get("ai_profile_stale") == before.get("ai_profile_stale")
        assert after.get("ai_profile_input_hash") == before.get("ai_profile_input_hash")
        assert "communication_note" not in after
        assert session.get(Candidate, candidate_id).communication_note == NOTE


@pytest.mark.asyncio
async def test_note_is_not_searchable(tmp_path: Path) -> None:
    """沟通记录不得进入索引（否则等于把主观判断喂给检索）。"""
    _services, client, candidate_id, _revision_id = await _import_candidate(tmp_path)
    try:
        assert (await _save_note(client, candidate_id, NOTE)).status_code == 200
        # 对照：简历正文里的词能搜到，说明检索链路本身是通的。
        hit = await client.post(
            "/api/search/candidates",
            json={"query": "Python", "filters": {}, "limit": 20},
            headers=HEADERS,
        )
        assert hit.json()["items"], "对照搜索应当命中，否则这条断言没有意义"

        searched = await client.post(
            "/api/search/candidates",
            json={"query": "性格偏内向 加班有顾虑", "filters": {}, "limit": 20},
            headers=HEADERS,
        )
        assert searched.status_code == 200
        assert searched.json()["items"] == []
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_note_can_be_cleared_and_missing_candidate_is_404(tmp_path: Path) -> None:
    _services, client, candidate_id, _revision_id = await _import_candidate(tmp_path)
    try:
        assert (await _save_note(client, candidate_id, NOTE)).status_code == 200

        cleared = await _save_note(client, candidate_id, "   ")
        assert cleared.status_code == 200
        assert cleared.json()["communication_note"] is None

        missing = await _save_note(client, "does-not-exist", NOTE)
        assert missing.status_code == 404
        assert missing.json()["code"] == "E_CANDIDATE_NOT_FOUND"
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_note_filter_matches_substring_and_excludes_others(tmp_path: Path) -> None:
    """沟通文本按子串筛选：命中的人出现，没写该词的另一个人不出现。"""
    _services, client, (first_id, second_id) = await _import_two_candidates(tmp_path)
    try:
        assert (await _save_note(client, first_id, "沟通主动，愿意出差")).status_code == 200
        assert (await _save_note(client, second_id, "性格内向，需要多推一把")).status_code == 200

        matched = await _search_note(client, "沟通主动")
        assert matched.status_code == 200
        assert [item["candidate_id"] for item in matched.json()["items"]] == [first_id]
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_note_filter_is_case_insensitive_and_treats_percent_literally(tmp_path: Path) -> None:
    """大小写不敏感；`%` 是普通字符，不能被当成通配符。"""
    _services, client, (first_id, second_id) = await _import_two_candidates(tmp_path)
    try:
        assert (await _save_note(client, first_id, "英文简历 Recruiter OK，涨薪 20%")).status_code == 200
        assert (await _save_note(client, second_id, "性格内向")).status_code == 200

        # 大小写不敏感：全小写的 recruiter 也要命中备注里大写的 Recruiter。
        lower = await _search_note(client, "recruiter")
        assert [item["candidate_id"] for item in lower.json()["items"]] == [first_id]

        # 转义回归守卫：输入 `%` 只应命中备注里真的含 `%` 的那个人；若被当成通配符，
        # 两个写了备注的人都会命中，「精确筛选」就退化成「匹配所有人」。
        percent = await _search_note(client, "%")
        assert percent.status_code == 200
        assert [item["candidate_id"] for item in percent.json()["items"]] == [first_id]
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_blank_note_filter_is_ignored(tmp_path: Path) -> None:
    """空白输入等价于没有该条件，不能变成「匹配所有写了备注的人」。"""
    _services, client, (first_id, second_id) = await _import_two_candidates(tmp_path)
    try:
        assert (await _save_note(client, first_id, "沟通主动")).status_code == 200
        # second_id 没有备注；若空白输入被当成硬条件，它就会被筛掉。
        for blank in ("", "   "):
            result = await _search_note(client, blank)
            assert result.status_code == 200
            assert {item["candidate_id"] for item in result.json()["items"]} == {first_id, second_id}
    finally:
        await client.aclose()
