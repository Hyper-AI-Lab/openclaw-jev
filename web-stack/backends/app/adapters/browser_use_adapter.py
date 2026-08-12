"""browser-use interactive agent with clear unavailable response."""
from __future__ import annotations

import os
from typing import Any, Dict


async def browser_use_run(task: str, *, start_url: str | None = None) -> Dict[str, Any]:
    try:
        from browser_use import Agent

        # browser-use typically needs an LLM; wire via env if present
        api_key = os.environ.get("OPENAI_API_KEY") or os.environ.get("NVIDIA_API_KEY")
        if not api_key:
            return {
                "ok": False,
                "backend": "browser_use",
                "error": "browser_use needs OPENAI_API_KEY or NVIDIA_API_KEY; prefer OpenClaw browser tool",
                "task": task,
                "start_url": start_url,
            }

        # Prefer modern Agent API when available; otherwise fail soft.
        try:
            from langchain_openai import ChatOpenAI

            llm = ChatOpenAI(model=os.environ.get("BROWSER_USE_MODEL", "gpt-4o-mini"), api_key=api_key)
            agent = Agent(task=task if not start_url else f"Start at {start_url}. {task}", llm=llm)
            result = await agent.run()
            return {
                "ok": True,
                "backend": "browser_use",
                "task": task,
                "start_url": start_url,
                "result": str(result)[:50_000],
            }
        except Exception as inner:
            return {
                "ok": False,
                "backend": "browser_use",
                "error": str(inner)[:500],
                "hint": "Use OpenClaw `browser` tool or RMP browser_automation catalog",
                "task": task,
            }
    except Exception as exc:
        return {
            "ok": False,
            "backend": "browser_use",
            "error": f"unavailable: {exc}",
            "hint": "Prefer OpenClaw browser tool; install browser-use for this endpoint",
            "task": task,
            "start_url": start_url,
        }
