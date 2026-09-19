import asyncio, sys
from pathlib import Path
sys.path.insert(0, "src")
from kerui_recruit.sidecar import build_settings, RuntimeArgs
from kerui_recruit.providers.factory import build_providers
from kerui_recruit.db.session import create_engine_for
from kerui_recruit.db.models import JdRevision
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

async def main():
    settings = build_settings(RuntimeArgs(
        host="127.0.0.1", port=43127, token="0" * 64,
        data_root=Path(r"C:\Users\Nl\Desktop\安装包+源码\candidate_pool\.dev-data"),
    ))
    bundle = build_providers(settings)
    jd_parser = bundle.jd_parser
    print("parser type:", type(jd_parser).__name__)

    engine = create_engine_for(Path(r"C:\Users\Nl\Desktop\安装包+源码\candidate_pool\.dev-data\db\recruit.sqlite3"))
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory() as s:
        rev = s.get(JdRevision, "01a08a1d-6840-761b-94f1-3f0c9465250b")
        text = rev.source_text
        print("JD 标题(原文前60字):", (text or "")[:60].replace("\n", " "))

    parsed = await jd_parser.parse_jd(text or "")
    print("\n=== 解析结果 ===")
    print("direction =", repr(parsed.direction))
    print("industry =", repr(parsed.industry))
    print("ai_category =", repr(parsed.ai_category))
    print("required_skills =", parsed.required_skills)
    print("core_duties =", parsed.core_duties[:3])

asyncio.run(main())
