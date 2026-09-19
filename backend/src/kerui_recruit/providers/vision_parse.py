from __future__ import annotations

import base64
import logging

import httpx
from pydantic import ValidationError

from kerui_recruit.jd.structured import ParsedJd
from kerui_recruit.providers.errors import ProviderError, map_http_error
from kerui_recruit.providers.generation_tasks import (
    render_jd_vision_parse_prompt,
    render_resume_vision_parse_prompt,
)
from kerui_recruit.providers.ocr import rasterize_pdf
from kerui_recruit.resumes.structured import ParsedResume

logger = logging.getLogger(__name__)

# 复用文本解析的字段定义，只把末尾的「原文文本」这一段换成图片指令；
# 方向词表、字数口径、硬条件口径等占位符由渲染函数统一填充。
_VISION_RESUME_INSTRUCTION = (
    "请根据下方图片（简历扫描件/截图，按页序）解析为上述 JSON。"
    "这些图片是同一份简历的不同页，请合并输出一个 JSON 对象，不要分页输出，只输出 JSON 对象本身。"
)
_VISION_JD_INSTRUCTION = (
    "请根据下方图片（JD 扫描件/截图，按页序）解析为上述 JSON。"
    "这些图片是同一份 JD 的不同页，请合并输出一个 JSON 对象，不要分页输出，只输出 JSON 对象本身。"
)
_VISION_RESUME_PROMPT = render_resume_vision_parse_prompt(_VISION_RESUME_INSTRUCTION)
_VISION_JD_PROMPT = render_jd_vision_parse_prompt(_VISION_JD_INSTRUCTION)

_IMAGE_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
    ".bmp": "image/bmp",
}

_PAGE_TIMEOUT_SECONDS = 300.0


def _mime_for(filename: str) -> str:
    suffix = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if suffix == "pdf":
        return "application/pdf"
    return _IMAGE_MIME.get(f".{suffix}", "application/octet-stream")


class VisionStructuredParser:
    """Use a vision model to directly structure a document image into JSON."""

    def __init__(self, llm) -> None:
        self._llm = llm

    async def parse_resume(self, content: bytes, filename: str) -> ParsedResume:
        return await self._complete_structured(
            _VISION_RESUME_PROMPT, content, filename, ParsedResume
        )

    async def parse_jd(self, content: bytes, filename: str) -> ParsedJd:
        return await self._complete_structured(
            _VISION_JD_PROMPT, content, filename, ParsedJd
        )

    def _rasterize(self, content: bytes, filename: str) -> list[tuple[int, bytes]]:
        if _mime_for(filename) == "application/pdf":
            return rasterize_pdf(content)
        return [(0, content)]

    async def _complete_structured(
        self,
        prompt: str,
        content: bytes,
        filename: str,
        response_model: type,
    ):
        images = self._rasterize(content, filename)
        parts: list[dict] = [{"type": "text", "text": prompt}]
        mime = _mime_for(filename)
        if mime == "application/pdf":
            mime = "image/png"
        for _, image in images:
            data_url = f"data:{mime};base64,{base64.b64encode(image).decode('ascii')}"
            parts.append({"type": "image_url", "image_url": {"url": data_url}})
        text = await self._llm.complete_text(
            [{"role": "user", "content": parts}]
        )
        try:
            return response_model.model_validate_json(_extract_json(text))
        except ValidationError as error:
            raise ProviderError(
                code="E_VISION_SCHEMA", retryable=True, user_message="视觉解析结果不符合结构要求",
            ) from error


def _extract_json(text: str) -> str:
    """从视觉模型输出中提取 JSON 对象，兼容 markdown 代码块与前后多余文本。"""
    text = (text or "").strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    if not text.startswith("{"):
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            text = text[start : end + 1]
    return text
