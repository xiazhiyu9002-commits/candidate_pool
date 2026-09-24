"""交互式画像生成的阶段性反馈（SSE）。

「重新生成画像」最坏 150 秒（`REGEN_TIMEOUT_SECONDS`）。7.5 已把它收成「有界等待 + 明确失败
文案」，但等待期间界面只有一个转圈——使用者分不清「还有 10 秒」和「刚开始第二次模型调用」，
而这两者的预期等待差一倍。这里把生成阶段真推给前端：

| stage | 含义 |
| --- | --- |
| `loading` | 正在读取原文（岗位/简历的最新 READY 版本） |
| `draft` | 正在生成初稿（**第一次**模型调用） |
| `repair` | 初稿未过校验，正在定向重写（**第二次、也是最后一次**模型调用） |

阶段名由生成层（`providers/profile_pair.py`）发出，中文文案在这里贴——生成层不该关心界面措辞。
事件体与 BD 助手同形（`{stage, message}`），前端只负责显示。

**为什么走同一个路径的内容协商，而不是新开 `-stream` 路径**：新路径会改变
`scripts/api_inventory_2026_09_21.py` 导出的路由清单，而「未覆盖路由 = 0」是 10.2 的验收闸门。
这条 SSE 只是同一个处理器的传输变体，不是新能力，客户端按 `Accept: text/event-stream` 选择。

**为什么错误要以事件形式回传**：SSE 响应头一旦发出，HTTP 状态码就再也改不了，
所以 404/504/502 必须在流里以 `error` 事件给出（前端按同一套 code/message 呈现），
否则客户端只会看到一条被截断的流，错误信息全部丢失。
"""
from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import Request
from fastapi.responses import StreamingResponse

from kerui_recruit.api.errors import ApiError

# 生成器的形状：接一个同步阶段回调，返回最终结果。
Generate = Callable[[Callable[[str], None]], Awaitable[dict]]

# 阶段 → 界面文案。文案只在这里维护一份（流式与非流式的交互都是同一个入口）。
_STAGE_MESSAGES = {
    "loading": "正在读取原文…",
    "draft": "正在生成画像初稿…",
    "repair": "初稿未通过校验，正在定向重写（第二次模型调用，会更久）…",
}


def wants_event_stream(request: Request) -> bool:
    """客户端是否要求流式（阶段性反馈）。"""
    return "text/event-stream" in (request.headers.get("accept") or "")


def sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def profile_generation_stream(
    generate: Generate,
    *,
    translate: Callable[[BaseException], ApiError],
    timeout_seconds: float,
) -> StreamingResponse:
    """把一次画像生成包成 SSE：先推阶段，最后推 `result` 或 `error`。

    ``translate`` 必须与同步路径用**同一个**异常→ApiError 映射，否则同一个失败在
    流式与非流式下会给出不同的错误码/文案。
    """
    queue: asyncio.Queue = asyncio.Queue()

    async def run() -> None:
        try:
            result = await asyncio.wait_for(generate(queue.put_nowait), timeout=timeout_seconds)
        except BaseException as error:  # noqa: BLE001 — 分类交给 translate，这里只负责传递
            await queue.put(("__error__", error))
            return
        await queue.put(("__result__", result))

    async def body():
        task = asyncio.create_task(run())
        try:
            while True:
                item = await queue.get()
                if isinstance(item, tuple):
                    kind, payload = item
                    if kind == "__result__":
                        yield sse("result", {"type": "result", **dict(payload)})
                    else:
                        error = translate(payload)
                        yield sse("error", {
                            "type": "error",
                            "status": error.status_code,
                            "code": error.code,
                            "message": error.message,
                            "details": _jsonable(error.details),
                        })
                    break
                yield sse("progress", {
                    "type": "progress",
                    "stage": item,
                    "message": _STAGE_MESSAGES.get(item, "正在生成画像…"),
                })
        finally:
            # 客户端断开时直接取消后台任务，别再烧模型调用。
            # 这里**不能** `await task`：在已关闭的异步生成器里 await 会抛
            # 「async generator ignored GeneratorExit」，把一次正常的断开变成 500。
            task.cancel()

    return StreamingResponse(
        body(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _jsonable(value: Any) -> Any:
    """details 必须是可 JSON 序列化的（SSE 事件体要整体 dumps）。"""
    if value is None or isinstance(value, (str, int, float, bool, list, dict)):
        return value
    return [item if isinstance(item, dict) else str(item) for item in value]
