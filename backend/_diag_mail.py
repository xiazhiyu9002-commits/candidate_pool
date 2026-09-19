import sys
from pathlib import Path
from datetime import datetime
sys.path.insert(0, "src")
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker
from kerui_recruit.sidecar import build_settings, RuntimeArgs
from kerui_recruit.db.session import create_engine_for
from kerui_recruit.db.models import DailyFollowupState, CandidateJobCase, CaseEvent
from kerui_recruit.daily_followup.service import SHANGHAI

root = Path(r"C:\Users\Nl\Desktop\安装包+源码\candidate_pool\.dev-data")
settings = build_settings(RuntimeArgs(host="127.0.0.1", port=43127, token="0"*64, data_root=root))

now_sh = datetime.now(SHANGHAI).replace(tzinfo=None)
print("=== 邮箱诊断 ===")
print("当前上海时间:", now_sh.strftime("%Y-%m-%d %H:%M:%S"), "(周", now_sh.weekday()+1, ")")
print("smtp_host:", settings.smtp_host)
print("smtp_port:", settings.smtp_port)
print("smtp_account:", settings.smtp_account)
print("smtp_auth_code 是否配置:", bool(settings.smtp_auth_code))
print("smtp_ssl:", settings.smtp_ssl)
print("reminder_to:", settings.reminder_to)
print("smtp_enabled:", settings.smtp_enabled)
print("daily_followup_enabled:", settings.daily_followup_enabled)
print("mail_enabled:", settings.mail_enabled)

e = create_engine_for(root / "db" / "recruit.sqlite3")
F = sessionmaker(e, expire_on_commit=False)
with F() as s:
    st = s.scalar(select(DailyFollowupState).limit(1))
    print("\n=== DailyFollowupState ===")
    print("state 存在:", st is not None)
    if st:
        print("last_evening_date:", st.last_evening_date)
        print("last_morning_date:", st.last_morning_date)
    print("今天日期:", now_sh.date().isoformat())

    cases = s.scalars(select(CandidateJobCase).where(CandidateJobCase.deleted_at.is_(None))).all()
    print("\n=== 招聘案例数据 ===")
    print("case 总数(未删除):", len(cases))
    evs = s.scalars(select(CaseEvent)).all()
    from collections import Counter
    print("事件类型分布:", dict(Counter(e.event_type for e in evs)))
