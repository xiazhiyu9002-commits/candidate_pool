from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field

from kerui_recruit.api.errors import ApiError
from kerui_recruit.api.services import AppServices
from kerui_recruit.daily_followup.service import SHANGHAI

router = APIRouter(prefix="/api/daily-followup", tags=["daily-followup"])

# 「今日待办」的两类系统项：同一 case 只会落在其中一类，因此 item_key 唯一。
_FOLLOWUP = "followup"
_INTERVIEW = "interview"


class TodoCheckRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item_key: str = Field(min_length=1, max_length=120)
    done: bool = True


def _service(request: Request):
    services: AppServices = request.app.state.services
    service = services.daily_followup_service
    if service is None:
        raise ApiError(503, "E_DAILY_FOLLOWUP_UNAVAILABLE", "每日待跟进服务不可用")
    return service


def _entry(*, item: dict, category: str, checked: set[str], day_precision: bool) -> dict:
    """组装一条待办项；``item_key`` 是勾选记录的稳定键，与展示字段无关。"""
    item_key = f"{category}:{item['case_id']}"
    entry = {
        "name": item["name"],
        "company": item["company"],
        "title": item["title"],
        "item_key": item_key,
        "done": item_key in checked,
    }
    # 追反馈精确到天（面试反馈是「哪一天之前要跟进」），待面试精确到分钟。
    if day_precision:
        entry["date"] = item["time"][:10]
    else:
        entry["time"] = item["time"]
    return entry


@router.get("/today")
def today(request: Request) -> dict:
    """首页「今日待办」：追反馈 / 待面试（系统项，当日勾选）+ 我的提醒（用户建的）。"""
    service = _service(request)
    services: AppServices = request.app.state.services

    now_sh = datetime.now(SHANGHAI).replace(tzinfo=None)
    data = service.gather(now_sh)
    checked = service.checked_item_keys(now_sh.date().isoformat())

    # 追反馈：推荐未反馈 + 面试未反馈，时间精确到「天」。
    followup = [
        _entry(item=item, category=_FOLLOWUP, checked=checked, day_precision=True)
        for item in data["recommended_no_feedback"] + data["interview_no_feedback"]
    ]

    # 待面试：今天 + 明天，时间精确到「分钟」。
    interview = [
        _entry(item=item, category=_INTERVIEW, checked=checked, day_precision=False)
        for item in data["today_interview"] + data["tomorrow_interview"]
    ]

    # 我的提醒：使用者从人才库建立的待办，无日期，勾选即完成并移出。
    reminders = (
        services.candidate_reminder_service.list_open()
        if services.candidate_reminder_service is not None
        else []
    )

    return {"followup": followup, "interview": interview, "reminders": reminders}


@router.post("/check")
def check_todo(request: Request, body: TodoCheckRequest) -> dict:
    """勾选/取消勾选一条系统待办项。

    勾选只表示「今天处理过」，**不代表任务完成**；记录按当天存储，次日自动回到未勾选。
    """
    service = _service(request)

    day = datetime.now(SHANGHAI).replace(tzinfo=None).date().isoformat()
    service.set_checked(day=day, item_key=body.item_key, done=body.done)
    return {"date": day, "item_key": body.item_key, "done": body.done}
