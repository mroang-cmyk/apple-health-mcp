"""
Health data ingestion endpoint.
Receives data from iOS Shortcuts and stores in Redis.
"""
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, unquote
from upstash_redis import Redis
from datetime import datetime, timedelta
import json
import os
import re

API_KEY = os.environ.get("API_KEY", "")

redis = Redis(
    url=os.environ.get("UPSTASH_REDIS_REST_URL"),
    token=os.environ.get("UPSTASH_REDIS_REST_TOKEN")
)


def check_auth(headers) -> bool:
    if not API_KEY:
        return True
    auth = headers.get("Authorization", "")
    return auth == f"Bearer {API_KEY}"


NUM_RE = re.compile(r"^\s*([-+]?\d[\d\s\u00a0\u202f]*(?:[.,]\d+)?)\s*[^\d]*$")


def to_number(v: str):
    """Parse a number from iOS Shortcuts text.
    Handles Norwegian/European formatting ("45,3"), thousand separators
    ("8 432") and trailing units ("45 ms", "62 count/min", "8 432 skritt")."""
    m = NUM_RE.match(v)
    if not m:
        return None
    s = m.group(1)
    for sep in (" ", "\u00a0", "\u202f"):
        s = s.replace(sep, "")
    s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def parse_values(raw: str) -> list:
    """Parse newline-separated values from iOS Shortcuts."""
    decoded = unquote(raw).replace("\r\n", "\n").replace("\r", "\n")
    values = []
    for v in decoded.split("\n"):
        v = v.strip()
        if v:
            n = to_number(v)
            values.append(n if n is not None else v)
    return values


def compute_hr_zones(values: list) -> dict:
    """
    Calculate time spent in each heart rate zone.
    Zones based on typical training thresholds.
    """
    nums = [v for v in values if isinstance(v, (int, float))]
    if not nums:
        return {}

    zones = {
        "rest": 0,      # < 100 bpm
        "light": 0,     # 100-120 bpm (yoga, walking)
        "moderate": 0,  # 120-140 bpm (strength, easy cardio)
        "hard": 0,      # 140-160 bpm (tempo, harder cardio)
        "max": 0        # 160+ bpm (intervals, sprints)
    }

    for hr in nums:
        if hr < 100:
            zones["rest"] += 1
        elif hr < 120:
            zones["light"] += 1
        elif hr < 140:
            zones["moderate"] += 1
        elif hr < 160:
            zones["hard"] += 1
        else:
            zones["max"] += 1

    total = len(nums)
    return {
        "zones": zones,
        "zone_pct": {k: round(v / total * 100) for k, v in zones.items()},
        "training_load": zones["moderate"] + zones["hard"] + zones["max"],
        "high_intensity": zones["hard"] + zones["max"]
    }


def compute_sleep_stats(values: list) -> dict:
    """Analyze sleep stage distribution."""
    stages = {"REM": 0, "Core": 0, "Deep": 0, "Awake": 0}
    for v in values:
        if isinstance(v, str):
            s = v.lower()
            if "rem" in s:
                stages["REM"] += 1
            elif "core" in s or "light" in s or "kjerne" in s or "lett" in s:
                stages["Core"] += 1
            elif "deep" in s or "dyp" in s:
                stages["Deep"] += 1
            elif "awake" in s or "wake" in s or "våken" in s or "vaken" in s:
                stages["Awake"] += 1

    total = sum(stages.values())
    if total == 0:
        return {"values": values}

    fragmentation = round(stages["Awake"] / total * 100, 1)
    quality = "good" if fragmentation < 20 and stages["REM"] > 0 and stages["Deep"] > 0 else \
              "fair" if fragmentation < 35 else "poor"

    return {
        "stages": stages,
        "fragmentation_pct": fragmentation,
        "quality": quality,
        "has_rem": stages["REM"] > 0,
        "has_deep": stages["Deep"] > 0
    }


def compute_stats(values: list, key: str = "") -> dict:
    """Compute statistics for health samples."""
    key_lower = key.lower().strip()

    if key_lower == "sleep":
        return compute_sleep_stats(values)

    nums = [v for v in values if isinstance(v, (int, float))]
    if not nums:
        return {"count": len(values)}

    # Cumulative metrics: daily totals from Watch (filtered in iOS Shortcuts)
    # Steps, Exercise, Active Energy use "Group by: Day" + Watch source filter
    cumulative_metrics = ["steps", "exercise", "activeenergy"]

    if key_lower in cumulative_metrics:
        return {
            "total": round(max(nums)),
            "sources": len(nums)
        }

    # Discrete metrics: compute full statistics
    result = {
        "avg": round(sum(nums) / len(nums), 2),
        "min": round(min(nums), 2),
        "max": round(max(nums), 2),
        "count": len(nums)
    }

    # Add HR zones for heart rate data
    if key_lower == "heartrate":
        result["hr_zones"] = compute_hr_zones(nums)

    return result


class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        if not check_auth(self.headers):
            self.send_response(401)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": "unauthorized"}).encode())
            return

        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length).decode("utf-8")

        form_data = parse_qs(body)
        # Data from iOS Shortcuts is "last 1 day" = yesterday's data
        yesterday = datetime.now() - timedelta(days=1)
        date_key = yesterday.strftime("%Y-%m-%d")
        redis_key = f"health:{date_key}"

        existing = redis.get(redis_key)
        health_data = json.loads(existing) if existing else {}

        for key, values in form_data.items():
            key = key.strip()
            if not key:
                continue
            raw = values[0] if values else ""
            parsed = parse_values(raw)
            health_data[key] = compute_stats(parsed, key)

        health_data["_updated"] = datetime.now().isoformat()
        redis.set(redis_key, json.dumps(health_data))

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({
            "ok": True,
            "date": date_key,
            "keys": list(form_data.keys())
        }).encode())

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({
            "endpoint": "ingest",
            "method": "POST",
            "description": "Receives health data from iOS Shortcuts"
        }).encode())
