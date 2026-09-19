#!/usr/bin/env python3
"""发布方离线生成 Ed25519 签名目录信封。

私钥仅由发布方持有，绝不进入安装包。脚本只读取私钥用于签名，不打印、不写出私钥。

用法：
    py -3.12 scripts/sign_ai_provider_catalog.py \\
        --catalog backend/src/kerui_recruit/providers/ai/provider_catalog.builtin.json \\
        --private-key-b64 <base64-encoded-ed25519-private-key> \\
        --output catalog.signed.json
"""
from __future__ import annotations

import argparse
import base64
import json
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def canonical_json(catalog: dict) -> bytes:
    return json.dumps(catalog, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Sign an AI provider catalog envelope")
    parser.add_argument("--catalog", required=True, help="path to the catalog JSON")
    parser.add_argument("--private-key-b64", required=True, help="base64 Ed25519 private key")
    parser.add_argument("--output", required=True, help="output envelope path")
    args = parser.parse_args()

    catalog = json.loads(Path(args.catalog).read_text(encoding="utf-8"))
    private_key = Ed25519PrivateKey.from_private_bytes(base64.b64decode(args.private_key_b64))
    signature = private_key.sign(canonical_json(catalog))

    envelope = {
        "catalog": catalog,
        "signature": base64.b64encode(signature).decode("ascii"),
    }
    Path(args.output).write_text(
        json.dumps(envelope, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
