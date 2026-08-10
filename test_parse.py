import json
from datetime import datetime, timezone

msg_ts = "2026-03-19T01:00:08.818Z"
print(f"type before: {type(msg_ts)}")

if isinstance(msg_ts, str):
    try:
        dt = datetime.fromisoformat(msg_ts.replace('Z', '+00:00'))
        msg_ts = dt.timestamp() * 1000
    except Exception as e:
        print(f"Error: {e}")
        msg_ts = 0

print(f"type after: {type(msg_ts)}")
print(f"value: {msg_ts}")
