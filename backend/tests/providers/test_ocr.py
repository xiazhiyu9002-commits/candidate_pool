from __future__ import annotations

import base64
import json

import httpx
import pymupdf
import pytest

from kerui_recruit.providers.ocr import OpenAICompatibleOCRProvider, rasterize_pdf


def _blank_pdf_bytes() -> bytes:
    document = pymupdf.open()
    page = document.new_page()
    page.insert_text((72, 72), "scanned resume")
    payload = document.tobytes()
    document.close()
    return payload


def test_rasterize_pdf_returns_one_png_per_page() -> None:
    images = rasterize_pdf(_blank_pdf_bytes())
    assert len(images) == 1
    assert images[0][0] == 0
    assert images[0][1].startswith(b"\x89PNG")


def test_rasterize_pdf_supports_page_selection() -> None:
    document = pymupdf.open()
    document.new_page()
    document.new_page()
    document.new_page()
    payload = document.tobytes()
    document.close()

    images = rasterize_pdf(payload, page_indexes=[0, 2])

    assert [index for index, _ in images] == [0, 2]
    assert all(data.startswith(b"\x89PNG") for _, data in images)


@pytest.mark.asyncio
async def test_extract_posts_vision_request_and_returns_text() -> None:
    captured: dict = {}

    class FakeLlm:
        async def complete_text(self, messages, temperature=None, **kwargs):
            captured["messages"] = messages
            return "张三 本科 6年"

    provider = OpenAICompatibleOCRProvider(FakeLlm())

    result = await provider.extract(_blank_pdf_bytes(), "resume.pdf")

    assert result == "张三 本科 6年"
    content = captured["messages"][0]["content"]
    assert content[0]["type"] == "text"
    image_url = content[1]["image_url"]["url"]
    assert image_url.startswith("data:image/png;base64,")
    decoded = base64.b64decode(image_url.split(",", 1)[1])
    assert decoded.startswith(b"\x89PNG")
