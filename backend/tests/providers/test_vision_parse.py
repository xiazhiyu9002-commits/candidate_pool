from __future__ import annotations

import json
from typing import Any

import pymupdf
import pytest

from kerui_recruit.providers.vision_parse import (
    _MAX_PAGES_PER_CALL,
    VisionStructuredParser,
)
from kerui_recruit.resumes.structured import ParsedResume


def _pdf_bytes(pages: int) -> bytes:
    document = pymupdf.open()
    for index in range(pages):
        page = document.new_page()
        page.insert_text((72, 72), f"page {index}")
    payload = document.tobytes()
    document.close()
    return payload


class RecordingLlm:
    """记录每次请求携带的图片数量，并按调用序返回预设 JSON。"""

    def __init__(self, payloads: list[dict[str, Any]]) -> None:
        self._payloads = payloads
        self.image_counts: list[int] = []
        self.data_urls: list[str] = []

    async def complete_text(self, messages, temperature=None, **kwargs):
        content = messages[0]["content"]
        images = [part for part in content if part["type"] == "image_url"]
        self.image_counts.append(len(images))
        self.data_urls.extend(part["image_url"]["url"] for part in images)
        payload = self._payloads[min(len(self.image_counts) - 1, len(self._payloads) - 1)]
        return json.dumps(payload, ensure_ascii=False)


@pytest.mark.asyncio
async def test_single_batch_when_pages_fit() -> None:
    llm = RecordingLlm([{"name": "张三", "skills": ["Python"]}])
    parser = VisionStructuredParser(llm)

    result = await parser.parse_resume(_pdf_bytes(_MAX_PAGES_PER_CALL), "resume.pdf")

    assert llm.image_counts == [_MAX_PAGES_PER_CALL]
    # 栅格化后按 JPEG 发送，data URL 的 mime 必须与编码一致。
    assert all(url.startswith("data:image/jpeg;base64,") for url in llm.data_urls)
    assert result.name == "张三"


@pytest.mark.asyncio
async def test_over_limit_pages_are_split_and_merged() -> None:
    """页数超过单次上限时拆批发送，并在本地合并成一份结果。

    原先所有页塞进一条消息，扫描件会被上游按「请求超出限制 / 图片过大」整份拒掉。
    """
    pages = _MAX_PAGES_PER_CALL + 2
    llm = RecordingLlm([
        {"name": "张三", "skills": ["Python"], "experiences": [{"company": "A", "title": "后端"}]},
        {"name": "", "skills": ["Python", "Java"], "experiences": [{"company": "B", "title": "架构"}]},
    ])
    parser = VisionStructuredParser(llm)

    result = await parser.parse_resume(_pdf_bytes(pages), "resume.pdf")

    assert llm.image_counts == [_MAX_PAGES_PER_CALL, 2]
    # 标量取第一个非空值（抬头信息在第一批里）。
    assert result.name == "张三"
    # 列表字段按批序拼接并去重。
    assert result.skills == ["Python", "Java"]
    assert [item.company for item in result.experiences] == ["A", "B"]


@pytest.mark.asyncio
async def test_resume_schema_accepts_minimal_payload() -> None:
    """守住测试用的最小 JSON 确实能过 ParsedResume 校验，避免夹具失真。"""
    llm = RecordingLlm([{"name": "张三"}])
    parser = VisionStructuredParser(llm)

    result = await parser.parse_resume(_pdf_bytes(1), "resume.pdf")

    assert isinstance(result, ParsedResume)
    assert result.name == "张三"
