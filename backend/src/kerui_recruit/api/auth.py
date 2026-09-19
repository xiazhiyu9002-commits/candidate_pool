from __future__ import annotations

import hmac

from fastapi import Request

from kerui_recruit.api.services import AppServices


def valid_session(request: Request, services: AppServices) -> bool:
    supplied = request.headers.get("X-Kerui-Session", "")
    expected = services.settings.session_token.get_secret_value()
    return bool(supplied) and hmac.compare_digest(supplied, expected)
