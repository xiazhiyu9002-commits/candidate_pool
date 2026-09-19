import sys, smtplib
from pathlib import Path
sys.path.insert(0, "src")
from kerui_recruit.sidecar import build_settings, RuntimeArgs

root = Path(r"C:\Users\Nl\Desktop\安装包+源码\candidate_pool\.dev-data")
settings = build_settings(RuntimeArgs(host="127.0.0.1", port=43127, token="0"*64, data_root=root))

print("=== SMTP 连接测试 ===")
print("host:", settings.smtp_host, "| port:", settings.smtp_port, "| ssl:", settings.smtp_ssl)
print("account:", settings.smtp_account, "| auth_code 已配置:", bool(settings.smtp_auth_code))
try:
    server = smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, timeout=20) if settings.smtp_ssl else smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=20)
    try:
        server.login(settings.smtp_account, settings.smtp_auth_code.get_secret_value())
        print("SMTP 登录成功 ✓")
    finally:
        try:
            server.quit()
        except Exception:
            server.close()
except Exception as e:
    print("SMTP 连接/登录失败 ✗:", e)
