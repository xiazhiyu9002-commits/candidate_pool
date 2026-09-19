"""供应商目录加载：内置 + 缓存 + 可选远程签名目录。

- ``load()`` 选择版本更新且有效的缓存目录，否则使用内置 JSON。
- ``refresh()`` 在未配置远程 URL/公钥时返回 ``disabled``，绝不抛错。
- 远程目录用 Ed25519 签名信封校验，仅允许更新模型 ID、能力声明、弃用状态和官方链接。
"""
from __future__ import annotations

import base64
import importlib.resources
import json
import os
from dataclasses import dataclass
from pathlib import Path

import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from kerui_recruit.providers.ai.catalog_models import ProviderCatalog

_BUILTIN_RESOURCE = "provider_catalog.builtin.json"
_MAX_CATALOG_BYTES = 1_000_000  # 1 MiB
_TIMEOUT_SECONDS = 5.0


def canonical_catalog_json(catalog: dict) -> bytes:
    """生成签名所用的规范化 UTF-8 JSON（与签名脚本保持一致）。"""
    return json.dumps(catalog, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def verify_signed_catalog(raw: bytes, public_key_b64: str) -> ProviderCatalog:
    """校验并解析 Ed25519 签名目录信封。校验失败抛出异常。"""
    if len(raw) > _MAX_CATALOG_BYTES:
        raise ValueError("catalog response exceeds 1 MiB")
    try:
        envelope = json.loads(raw)
        catalog_data = envelope["catalog"]
        signature_b64 = envelope["signature"]
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("catalog envelope is malformed") from error

    try:
        signature = base64.b64decode(signature_b64, validate=True)
        public_key = Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key_b64, validate=True))
    except (ValueError, TypeError) as error:
        raise ValueError("catalog signature or public key is invalid") from error

    public_key.verify(signature, canonical_catalog_json(catalog_data))
    return ProviderCatalog.model_validate(catalog_data)


@dataclass(frozen=True, slots=True)
class CatalogRefreshResult:
    status: str  # "updated" | "cached" | "builtin" | "disabled"
    active_version: int
    message: str = ""


class CatalogService:
    def __init__(self, cache_path: Path) -> None:
        self.cache_path = cache_path

    def load(self) -> ProviderCatalog:
        builtin = self._load_builtin()
        cached = self._load_cached()
        if cached is not None and cached.version > builtin.version:
            # 高版本缓存仍须通过内置目录结构约束检查，违规时回退内置目录。
            try:
                self._validate_remote_catalog(cached, builtin)
                return cached
            except ValueError:
                return builtin
        return builtin

    def refresh(
        self,
        *,
        url: str | None = None,
        public_key_b64: str | None = None,
        client: httpx.Client | None = None,
    ) -> CatalogRefreshResult:
        url = url if url is not None else os.environ.get("KERUI_AI_CATALOG_URL")
        public_key_b64 = public_key_b64 if public_key_b64 is not None else os.environ.get("KERUI_AI_CATALOG_PUBLIC_KEY_B64")
        active = self.load()
        if not url or not public_key_b64:
            return CatalogRefreshResult(status="disabled", active_version=active.version)
        if not url.startswith("https://"):
            return CatalogRefreshResult(status="builtin", active_version=active.version, message="catalog URL must be HTTPS")

        owner = client is None
        http = client or httpx.Client(timeout=_TIMEOUT_SECONDS, follow_redirects=False)
        try:
            response = http.get(url)
        except httpx.HTTPError as error:
            return CatalogRefreshResult(status="cached" if self._load_cached() is not None else "builtin",
                                        active_version=active.version, message=f"catalog fetch failed: {type(error).__name__}")
        finally:
            if owner:
                http.close()

        if response.status_code != 200:
            return CatalogRefreshResult(status="builtin", active_version=active.version, message=f"catalog HTTP {response.status_code}")
        try:
            candidate = verify_signed_catalog(response.content, public_key_b64)
        except Exception as error:
            return CatalogRefreshResult(status="builtin", active_version=active.version, message=f"catalog rejected: {error}")

        if candidate.version <= active.version:
            return CatalogRefreshResult(status="builtin", active_version=active.version, message="catalog version rollback rejected")
        try:
            self._validate_remote_catalog(candidate, self._load_builtin())
        except ValueError as error:
            return CatalogRefreshResult(status="builtin", active_version=active.version, message=f"catalog rejected: {error}")
        self._atomic_write(candidate)
        return CatalogRefreshResult(status="updated", active_version=candidate.version)

    def _validate_remote_catalog(self, candidate: ProviderCatalog, baseline: ProviderCatalog) -> None:
        """远程/缓存目录只能更新模型、推荐模型、能力/弃用状态与帮助链接，不得改结构。"""
        if set(candidate.providers) != set(baseline.providers):
            raise ValueError("remote catalog must not add or remove providers")
        if candidate.default_provider_id != baseline.default_provider_id:
            raise ValueError("remote catalog must not change default_provider_id")
        for provider_id, preset in candidate.providers.items():
            base = baseline.providers[provider_id]
            for field in ("base_url", "parameter_style", "label", "allowed_contexts", "subscription_warning"):
                if getattr(preset, field) != getattr(base, field):
                    raise ValueError(f"remote catalog must not change {field} for {provider_id}")

    def _load_builtin(self) -> ProviderCatalog:
        raw = importlib.resources.files("kerui_recruit.providers.ai").joinpath(_BUILTIN_RESOURCE).read_text(encoding="utf-8")
        return ProviderCatalog.model_validate_json(raw)

    def _load_cached(self) -> ProviderCatalog | None:
        if not self.cache_path.exists():
            return None
        try:
            raw = self.cache_path.read_text(encoding="utf-8")
            return ProviderCatalog.model_validate_json(raw)
        except Exception:
            return None

    def _atomic_write(self, catalog: ProviderCatalog) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.cache_path.with_suffix(".tmp")
        tmp.write_text(catalog.model_dump_json(), encoding="utf-8")
        tmp.replace(self.cache_path)
