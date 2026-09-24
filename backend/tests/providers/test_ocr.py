from __future__ import annotations

import base64

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


def test_rasterize_pdf_returns_one_jpeg_per_page() -> None:
    images = rasterize_pdf(_blank_pdf_bytes())
    assert len(images) == 1
    assert images[0][0] == 0
    assert images[0][1].startswith(b"\xff\xd8")


def test_rasterize_pdf_supports_page_selection() -> None:
    document = pymupdf.open()
    document.new_page()
    document.new_page()
    document.new_page()
    payload = document.tobytes()
    document.close()

    images = rasterize_pdf(payload, page_indexes=[0, 2])

    assert [index for index, _ in images] == [0, 2]
    assert all(data.startswith(b"\xff\xd8") for _, data in images)


def test_rasterize_pdf_caps_page_long_edge() -> None:
    """单页最长边必须受上限约束。

    实测 A4 在 DPI 200 下是 1654x2339，多页拼进一条消息会被上游按「图片过大」
    整份拒掉（E_API_INPUT）。这里断言上限真的生效。
    """
    from kerui_recruit.providers.ocr import _MAX_IMAGE_EDGE

    images = rasterize_pdf(_blank_pdf_bytes())
    pixmap = pymupdf.Pixmap(images[0][1])
    assert max(pixmap.width, pixmap.height) <= _MAX_IMAGE_EDGE


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
    # PDF 栅格化后按 JPEG 发送，data URL 的 mime 必须与编码一致。
    assert image_url.startswith("data:image/jpeg;base64,")
    decoded = base64.b64decode(image_url.split(",", 1)[1])
    assert decoded.startswith(b"\xff\xd8")
