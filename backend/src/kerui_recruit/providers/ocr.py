from __future__ import annotations

import base64
import io

import httpx
import pymupdf

from kerui_recruit.providers.errors import ProviderError, map_http_error

_OCR_PROMPT = "请完整提取图片中的文字，保留原始排版与换行，不要总结、翻译或补充内容。"

_IMAGE_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
    ".bmp": "image/bmp",
}

_DEFAULT_DPI = 200
_MAX_PAGES = 50
# 单个页面识别超时；每页独立控制，避免长文档无限占用。
_PAGE_TIMEOUT_SECONDS = 120.0


def rasterize_pdf(
    content: bytes,
    *,
    page_indexes: list[int] | None = None,
    dpi: int = _DEFAULT_DPI,
) -> list[tuple[int, bytes]]:
    """把 PDF 的指定页面渲染为 PNG，返回 (页码, 图片字节) 列表。

    未指定 ``page_indexes`` 时渲染全部页面；页数受 ``_MAX_PAGES`` 上限约束，
    超出部分直接忽略，避免异常大的文档被无限制地调用模型。
    """
    images: list[tuple[int, bytes]] = []
    with pymupdf.open(stream=io.BytesIO(content), filetype="pdf") as document:
        indexes = page_indexes if page_indexes is not None else list(range(document.page_count))
        for index in indexes:
            if index < 0 or index >= document.page_count:
                continue
            if len(images) >= _MAX_PAGES:
                break
            page = document[index]
            images.append((index, page.get_pixmap(dpi=dpi).tobytes("png")))
    return images


class OpenAICompatibleOCRProvider:
    """Vision-model OCR for scanned resumes via a generation task client."""

    def __init__(self, llm) -> None:
        self._llm = llm

    async def extract(self, content: bytes, filename: str) -> str:
        """识别整份文件，返回拼接后的文本（用于所有页面都需要 OCR 的场景）。"""
        if _mime_for(filename) == "application/pdf":
            images = [data for _, data in rasterize_pdf(content)]
        else:
            images = [content]
        texts = [await self._extract_image(image, filename) for image in images]
        return "\n".join(text.strip() for text in texts if text.strip())

    async def extract_pages(
        self,
        content: bytes,
        filename: str,
        page_indexes: list[int],
    ) -> list[str]:
        """只识别指定页面，按 ``page_indexes`` 顺序返回每页文本。"""
        if not page_indexes:
            return []
        if _mime_for(filename) == "application/pdf":
            rasterized = rasterize_pdf(content, page_indexes=page_indexes)
        else:
            rasterized = [(page_indexes[0], content)]
        results: list[str] = []
        for _, image in rasterized:
            results.append(await self._extract_image(image, filename))
        return results

    async def _extract_image(self, image: bytes, filename: str) -> str:
        mime = _mime_for(filename)
        if mime == "application/pdf":
            mime = "image/png"
        data_url = f"data:{mime};base64,{base64.b64encode(image).decode('ascii')}"
        try:
            text = await self._llm.complete_text(
                [{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": _OCR_PROMPT},
                        {"type": "image_url", "image_url": {"url": data_url}},
                    ],
                }]
            )
        except ProviderError:
            raise
        if not text or not text.strip():
            raise ProviderError(
                code="E_OCR_EMPTY",
                retryable=False,
                user_message="OCR 服务未识别到任何文字",
            )
        return text


def _map_ocr_http_error(response: httpx.Response) -> ProviderError:
    status = response.status_code
    # 视觉模型不支持图片输入时，多数兼容接口返回 400/422 并在消息中说明。
    if status in (400, 422):
        detail = ""
        try:
            payload = response.json()
            detail = str(payload.get("error", {}).get("message", ""))
        except (ValueError, AttributeError):
            pass
        lowered = detail.lower()
        if any(token in lowered for token in ("image", "vision", "图片", "视觉", "multimodal")):
            return ProviderError(
                code="E_OCR_UNSUPPORTED",
                retryable=False,
                user_message="当前模型不支持图片识别，请更换支持视觉的模型",
            )
    return map_http_error(status)


def _mime_for(filename: str) -> str:
    suffix = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if suffix == "pdf":
        return "application/pdf"
    return _IMAGE_MIME.get(f".{suffix}", "application/octet-stream")
