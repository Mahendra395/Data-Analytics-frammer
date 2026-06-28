"""Quick smoke-test for all API endpoints."""
import asyncio
import json
import sys
import urllib.request
import urllib.error

BASE = "http://127.0.0.1:8001/api/v1"

ENDPOINTS = [
    ("GET", "/kpis"),
    ("GET", "/monthly"),
    ("GET", "/channels"),
    ("GET", "/channels/users"),
    ("GET", "/users"),
    ("GET", "/languages"),
    ("GET", "/input-types"),
    ("GET", "/output-types"),
    ("GET", "/videos?limit=5"),
    ("GET", "/publishing/by-channel"),
    ("GET", "/publishing/by-channel/duration"),
    ("GET", "/quality/summary"),
    ("GET", "/forecast/total_uploaded"),
    ("GET", "/forecast/total_published"),
    ("GET", "/dimensions"),
    ("POST", "/query"),
]

QUERY_BODY = json.dumps({
    "sql": "SELECT COUNT(*) AS total FROM fact_video",
    "limit": 10
}).encode()


def hit(method: str, path: str) -> tuple[int, object]:
    url = BASE + path
    try:
        if method == "POST":
            req = urllib.request.Request(url, data=QUERY_BODY,
                                          headers={"Content-Type": "application/json"},
                                          method="POST")
        else:
            req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
            return resp.status, data
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        return e.code, body
    except Exception as exc:
        return 0, str(exc)


def summarise(data: object) -> str:
    if isinstance(data, dict):
        keys = list(data.keys())[:5]
        return "{" + ", ".join(f"{k}: {repr(data[k])[:30]}" for k in keys) + "}"
    if isinstance(data, list):
        return f"[{len(data)} items]"
    s = str(data)
    return s[:120]


GREEN = "\033[32m"
RED   = "\033[31m"
RESET = "\033[0m"

all_ok = True
for method, path in ENDPOINTS:
    status, data = hit(method, path)
    ok = 200 <= status < 300
    if not ok:
        all_ok = False
    icon = f"{GREEN}✓{RESET}" if ok else f"{RED}✗{RESET}"
    summary = summarise(data)
    print(f"{icon}  {method:4s} {path:<35s}  HTTP {status}  {summary}")

print()
print("All OK ✓" if all_ok else "Some endpoints FAILED ✗")
sys.exit(0 if all_ok else 1)
