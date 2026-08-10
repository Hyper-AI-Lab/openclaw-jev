import json
from datetime import datetime, timezone

line = '{"type":"message","id":"daee40c2","parentId":"43a22bdc","timestamp":"2026-03-19T01:00:08.818Z","message":{"role":"assistant","content":[{"type":"thinking","thinking":"..."}],"api":"google-generative-ai","provider":"google","model":"gemini-2.5-flash","usage":{"input":40879,"output":1909,"cacheRead":0,"cacheWrite":0,"totalTokens":42788,"cost":{"input":0.012263699999999999,"output":0.004772500000000001,"cacheRead":0,"cacheWrite":0,"total":0.0170362}},"stopReason":"stop","timestamp":1773881998653}}'
entry = json.loads(line)
msg_ts = entry.get("timestamp", entry.get("message", {}).get("timestamp", 0))

print(f"msg_ts initially: {msg_ts} (type {type(msg_ts)})")
# Convert string ISO dates to ms
if isinstance(msg_ts, str):
    try:
        dt = datetime.fromisoformat(msg_ts.replace('Z', '+00:00'))
        msg_ts = dt.timestamp() * 1000
    except Exception as e:
        print(f"Exception: {e}")
        msg_ts = 0

print(f"msg_ts finally: {msg_ts} (type {type(msg_ts)})")
