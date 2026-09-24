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

# 单页图像的最长边上限。实测 A4 页面在 DPI 200 下是 1654x2339、PNG 单页 300~600KB，
# 多页拼进一条消息后很容易顶到上游的「图片过大 / 请求超出限制」而被整份拒掉
# （表现为 E_API_INPUT：请求超出限制或格式不支持）。1568 是视觉模型常用的最优边长上限。
_MAX_IMAGE_EDGE = 1568
_JPEG_QUALITY = 85
# PDF 栅格化后的统一编码。调用方拼 data URL 时必须引用本常量，不要写死。
# 同样的页在 JPEG@1568 下体积约为 PNG@200 的一半，且边长落在通用上限内。
RASTER_IMAGE_MIME = "image/jpeg"


def _render_page(page: "pymupdf.Page", dpi: int, max_edge: int) -> bytes:
    """把一页渲染为 JPEG，并保证最长边不超过 ``max_edge``。"""
    scale = dpi / 72.0
    longest = max(page.rect.width, page.rect.height)
    if longest > 0:
        scale = min(scale, max_edge / longest)
    pixmap = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale))
    return pixmap.tobytes("jpg", jpg_quality=_JPEG_QUALITY)


def rasterize_pdf(
    content: bytes,
    *,
    page_indexes: list[int] | None = None,
    dpi: int = _DEFAULT_DPI,
    max_edge: int = _MAX_IMAGE_EDGE,
) -> list[tuple[int, bytes]]:
    """把 PDF 的指定页面渲染为 JPEG，返回 (页码, 图片字节) 列表。

    未指定 ``page_indexes`` 时渲染全部页面；页数受 ``_MAX_PAGES`` 上限约束，
    超出部分直接忽略，避免异常大的文档被无限制地调用模型。
    每页最长边受 ``max_edge`` 约束，保证单条消息里的图片总量可控。
    """
    images: list[tuple[int, bytes]] = []
    with pymupdf.open(stream=io.BytesIO(content), filetype="pdf") as document:
        indexes = page_indexes if page_indexes is not None else list(range(document.page_count))
        for index in indexes:
            if index < 0 or index >= document.page_count:
                continue
            if len(images) >= _MAX_PAGES:
                break
            images.append((index, _render_page(document[index], dpi, max_edge)))
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
            mime = RASTER_IMAGE_MIME
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
