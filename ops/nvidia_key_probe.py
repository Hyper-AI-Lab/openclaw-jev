#!/usr/bin/env python3
"""Live smoke test for all configured NVIDIA API keys."""
import json
import sys
import time

sys.path.insert(0, "/root/.openclaw/rmp")

import httpx

from app.llm.quota_broker import _load_env_keys, api_key_for_profile
from app.llm.usage_monitor import record_request

BASE = "https://integrate.api.nvidia.com/v1"
MODEL = "deepseek-ai/deepseek-v4-flash-0731"


def probe(profile_id: str) -> dict:
    key = api_key_for_profile(profile_id)
    t0 = time.time()
    try:
        with httpx.Client(base_url=BASE, timeout=30.0) as client:
            r = client.post(
                "/chat/completions",
                headers={"Authorization": f"Bearer {key}"},
                json={
                    "model": MODEL,
                    "messages": [{"role": "user", "content": "Reply with exactly: ok"}],
                    "max_tokens": 8,
                    "temperature": 0,
                },
            )
        ms = int((time.time() - t0) * 1000)
        if r.status_code == 200:
            body = r.json()
            text = (
                body.get("choices", [{}])[0]
                .get("message", {})
                .get("content", "")
            )
            usage = body.get("usage") or {}
            record_request(
                profile_id,
                "probe",
                input_tokens=int(usage.get("prompt_tokens") or 0),
                output_tokens=int(usage.get("completion_tokens") or 0),
                total_tokens=int(usage.get("total_tokens") or 0),
                model=MODEL,
            )
            return {
                "profile_id": profile_id,
                "status": "ok",
                "latency_ms": ms,
                "reply": str(text).strip()[:80],
            }
        record_request(profile_id, "probe", is_rate_limit=(r.status_code == 429))
        return {
            "profile_id": profile_id,
            "status": f"http_{r.status_code}",
            "latency_ms": ms,
            "detail": r.text[:120],
        }
    except Exception as exc:
        ms = int((time.time() - t0) * 1000)
        return {
            "profile_id": profile_id,
            "status": "error",
            "latency_ms": ms,
            "detail": str(exc)[:120],
        }


def main() -> int:
    keys = _load_env_keys()
    if not keys:
        print(json.dumps({"error": "no NVIDIA keys configured"}))
        return 1

    results = []
    for profile_id, _ in keys:
        results.append(probe(profile_id))
        time.sleep(2)

    ok = sum(1 for r in results if r["status"] == "ok")
    payload = {"model": MODEL, "ok": ok, "total": len(results), "results": results}
    print(json.dumps(payload, indent=2))
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
