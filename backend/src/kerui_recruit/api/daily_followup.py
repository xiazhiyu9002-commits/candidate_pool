from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Request

from kerui_recruit.api.errors import ApiError
from kerui_recruit.api.services import AppServices
from kerui_recruit.daily_followup.service import SHANGHAI

router = APIRouter(prefix="/api/daily-followup", tags=["daily-followup"])


@router.get("/today")
def today(request: Request) -> dict:
    """首页「今日待办」：左「追反馈」右「待面试」。"""
    services: AppServices = request.app.state.services
    service = services.daily_followup_service
    if service is None:
        raise ApiError(503, "E_DAILY_FOLLOWUP_UNAVAILABLE", "每日待跟进服务不可用")

    now_sh = datetime.now(SHANGHAI).replace(tzinfo=None)
    data = service.gather(now_sh)

    # 追反馈：推荐未反馈 + 面试未反馈，时间精确到「天」。
    followup: list[dict] = []
    for item in data["recommended_no_feedback"]:
        followup.append({
            "name": item["name"],
            "company": item["company"],
            "title": item["title"],
            "date": item["time"],
        })
    for item in data["interview_no_feedback"]:
        followup.append({
            "name": item["name"],
            "company": item["company"],
            "title": item["title"],
            "date": item["time"][:10],
        })

    # 待面试：今天 + 明天，时间精确到「分钟」。
    interview: list[dict] = []
    for item in data["today_interview"] + data["tomorrow_interview"]:
        interview.append({
            "name": item["name"],
            "company": item["company"],
            "title": item["title"],
            "time": item["time"],
        })

    return {"followup": followup, "interview": interview}
