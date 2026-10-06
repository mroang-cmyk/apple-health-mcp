"""
MCP Server for health data.
Exposes health metrics to Claude via Model Context Protocol (Streamable HTTP).

Patched for better compatibility with Claude custom connectors:
- Protocol version negotiation (supports 2024-11-05 through 2025-06-18)
- Notifications get 202 Accepted with empty body
- GET requests asking for an SSE stream get 405 (no server-initiated stream)
- ping, OPTIONS/CORS and empty or invalid bodies handled without crashing
"""
from http.server import BaseHTTPRequestHandler
from upstash_redis import Redis
from datetime import datetime, timedelta
from urllib.parse import urlparse, parse_qs
import json
import os

MCP_SECRET = os.environ.get("MCP_SECRET", "")
EXERCISE_DAYS_PER_WEEK = os.environ.get("EXERCISE_DAYS_PER_WEEK", "")

SUPPORTED_VERSIONS = ["2025-06-18", "2025-03-26", "2024-11-05"]
SERVER_INFO = {"name": "health", "version": "1.1.0"}

redis = Redis(
    url=os.environ.get("UPSTASH_REDIS_REST_URL"),
    token=os.environ.get("UPSTASH_REDIS_REST_TOKEN")
)


def check_secret(path: str) -> bool:
    if not MCP_SECRET:
        return True
    query = parse_qs(urlparse(path).query)
    return query.get("key", [""])[0].strip() == MCP_SECRET.strip()


def parse_exercise_routine() -> dict:
    """Parse exercise routine from env var."""
    if not EXERCISE_DAYS_PER_WEEK:
        return {}
    routine = {}
    for item in EXERCISE_DAYS_PER_WEEK.split(","):
        if ":" in item:
            k, v = item.split(":", 1)
            try:
                routine[k.strip()] = int(v.strip())
            except ValueError:
                pass
    return routine


def get_health_data(date_key: str) -> dict:
    data = redis.get(f"health:{date_key}")
    return json.loads(data) if data else {}


def get_cumulative_total(metric_data: dict) -> int:
    """Extract total from cumulative metric. Handles both storage formats."""
    if not metric_data:
        return 0
    if "total" in metric_data:
        return metric_data["total"]
    if "avg" in metric_data and "count" in metric_data:
        return round(metric_data["avg"] * metric_data["count"])
    return 0


def get_exercise_key(data: dict) -> str:
    """Handle iOS Shortcut naming quirk. Some configs have trailing space."""
    if "exercise " in data:
        return "exercise "
    return "exercise"


def extract_day_metrics(data: dict) -> dict:
    """Extract all health metrics from a day's data."""
    if not data:
        return None

    metrics = {}

    if "hrv" in data and data["hrv"].get("avg"):
        metrics["hrv"] = round(data["hrv"]["avg"], 1)

    if "heartRate" in data:
        hr = data["heartRate"]
        if hr.get("min"):
            metrics["resting_hr"] = round(hr["min"], 1)
        if "hr_zones" in hr and hr["hr_zones"].get("zone_pct"):
            metrics["hr_zones"] = hr["hr_zones"]["zone_pct"]

    if "sleep" in data:
        sleep = data["sleep"]
        metrics["sleep"] = {
            "quality": sleep.get("quality"),
            "fragmentation_pct": sleep.get("fragmentation_pct"),
            "has_deep": sleep.get("has_deep"),
            "has_rem": sleep.get("has_rem")
        }

    exercise_key = get_exercise_key(data)
    if exercise_key in data:
        metrics["exercise_min"] = get_cumulative_total(data[exercise_key])

    if "steps" in data:
        metrics["steps"] = get_cumulative_total(data["steps"])

    if "activeEnergy" in data:
        metrics["active_calories"] = get_cumulative_total(data["activeEnergy"])

    if "mindful" in data:
        metrics["mindful_min"] = get_cumulative_total(data["mindful"])

    if "respRate" in data and data["respRate"].get("avg"):
        metrics["respiratory_rate"] = round(data["respRate"]["avg"], 1)

    return metrics if metrics else None


def get_hrv_baseline(days: int = 14) -> dict:
    """Calculate HRV baseline from recent history."""
    hrv_values = []
    for i in range(1, days + 1):
        date = (datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d")
        data = get_health_data(date)
        if data and "hrv" in data and data["hrv"].get("avg"):
            hrv_values.append(data["hrv"]["avg"])
    if not hrv_values:
        return {"baseline": None, "days": 0}
    return {
        "baseline": round(sum(hrv_values) / len(hrv_values), 1),
        "days": len(hrv_values)
    }


# MCP Tools

def tool_get_today() -> str:
    date_key = datetime.now().strftime("%Y-%m-%d")
    data = get_health_data(date_key)
    if not data:
        return json.dumps({"error": "No data synced today. Run iOS shortcuts."})
    return json.dumps(data, indent=2)


def tool_get_trends(days: int = 7) -> str:
    results = {}
    for i in range(days):
        date = (datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d")
        data = get_health_data(date)
        metrics = extract_day_metrics(data)
        if metrics:
            results[date] = metrics
    if not results:
        return json.dumps({"error": f"No data for last {days} days."})
    return json.dumps(results, indent=2)


def tool_get_recovery_status() -> str:
    date_key = datetime.now().strftime("%Y-%m-%d")
    data = get_health_data(date_key)
    baseline = get_hrv_baseline()

    status = {
        "date": date_key,
        "weekly_routine": parse_exercise_routine() or None
    }

    today_metrics = extract_day_metrics(data)
    if today_metrics:
        status["today"] = today_metrics
        if "hrv" in today_metrics and baseline.get("baseline"):
            hrv = today_metrics["hrv"]
            status["hrv_vs_baseline"] = {
                "today": hrv,
                "baseline": baseline["baseline"],
                "baseline_days": baseline["days"],
                "pct_diff": round(((hrv - baseline["baseline"]) / baseline["baseline"]) * 100)
            }

    recent_days = {}
    for i in range(1, 4):
        day_key = (datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d")
        day_data = get_health_data(day_key)
        metrics = extract_day_metrics(day_data)
        if metrics:
            recent_days[f"day_minus_{i}"] = metrics
    if recent_days:
        status["recent_days"] = recent_days

    return json.dumps(status, indent=2)


TOOLS = [
    {
        "name": "get_today",
        "description": "Get raw health data for today. Returns unprocessed data as stored.",
        "inputSchema": {"type": "object", "properties": {}, "required": []}
    },
    {
        "name": "get_trends",
        "description": "Get health metrics over multiple days: HRV, resting HR, HR zones, sleep, exercise minutes, steps, active calories, mindful minutes, respiratory rate.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "days": {"type": "integer", "description": "Number of days (default 7)"}
            },
            "required": []
        }
    },
    {
        "name": "get_recovery_status",
        "description": "Get comprehensive recovery data: today's metrics (HRV, resting HR, HR zones, sleep, exercise, steps, calories, mindful minutes, respiratory rate) with HRV baseline comparison, plus last 3 days with full metrics for trend analysis. Includes weekly exercise routine.",
        "inputSchema": {"type": "object", "properties": {}, "required": []}
    }
]


def handle_tool_call(name: str, args: dict):
    if name == "get_today":
        return tool_get_today(), False
    if name == "get_trends":
        try:
            days = int((args or {}).get("days", 7))
        except (TypeError, ValueError):
            days = 7
        return tool_get_trends(max(1, min(days, 60))), False
    if name == "get_recovery_status":
        return tool_get_recovery_status(), False
    return json.dumps({"error": f"Unknown tool: {name}"}), True


def handle_rpc(msg: dict):
    """Return a JSON-RPC response dict, or None for notifications."""
    if not isinstance(msg, dict):
        return {"jsonrpc": "2.0", "id": None,
                "error": {"code": -32600, "message": "Invalid request"}}

    method = msg.get("method", "")
    req_id = msg.get("id")
    is_notification = "id" not in msg

    if is_notification:
        return None

    if method == "initialize":
        requested = (msg.get("params") or {}).get("protocolVersion", "")
        version = requested if requested in SUPPORTED_VERSIONS else SUPPORTED_VERSIONS[0]
        return {
            "jsonrpc": "2.0", "id": req_id,
            "result": {
                "protocolVersion": version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": SERVER_INFO
            }
        }
    if method == "ping":
        return {"jsonrpc": "2.0", "id": req_id, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": TOOLS}}
    if method == "tools/call":
        params = msg.get("params") or {}
        try:
            text, is_error = handle_tool_call(params.get("name", ""), params.get("arguments") or {})
        except Exception as e:  # keep the connector alive on data errors
            text, is_error = json.dumps({"error": str(e)}), True
        return {
            "jsonrpc": "2.0", "id": req_id,
            "result": {"content": [{"type": "text", "text": text}], "isError": is_error}
        }
    if method in ("resources/list", "prompts/list"):
        key = method.split("/")[0]
        return {"jsonrpc": "2.0", "id": req_id, "result": {key: []}}

    return {"jsonrpc": "2.0", "id": req_id,
            "error": {"code": -32601, "message": f"Unknown method: {method}"}}


class handler(BaseHTTPRequestHandler):
    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS, DELETE")
        self.send_header("Access-Control-Allow-Headers",
                         "Content-Type, Authorization, Mcp-Session-Id, Mcp-Protocol-Version")

    def send_json(self, data, status: int = 200):
        payload = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self._cors()
        self.end_headers()
        self.wfile.write(payload)

    def send_empty(self, status: int, extra_headers: dict = None):
        self.send_response(status)
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", "0")
        self._cors()
        self.end_headers()

    def do_OPTIONS(self):
        self.send_empty(204)

    def do_DELETE(self):
        self.send_empty(405, {"Allow": "GET, POST, OPTIONS"})

    def do_GET(self):
        if not check_secret(self.path):
            self.send_json({"error": "unauthorized"}, 401)
            return
        # MCP clients may GET with Accept: text/event-stream to open a
        # server-initiated stream. We don't offer one, so answer 405 as the spec allows.
        if "text/event-stream" in (self.headers.get("Accept") or ""):
            self.send_empty(405, {"Allow": "POST"})
            return
        # Plain browser check
        self.send_json({
            "name": SERVER_INFO["name"],
            "version": SERVER_INFO["version"],
            "description": "Personal health data from Apple Watch via iOS Shortcuts",
            "tools": TOOLS
        })

    def do_POST(self):
        if not check_secret(self.path):
            self.send_json({"error": "unauthorized"}, 401)
            return

        try:
            length = int(self.headers.get("Content-Length", 0) or 0)
            raw = self.rfile.read(length).decode("utf-8") if length else ""
            body = json.loads(raw) if raw else None
        except (ValueError, UnicodeDecodeError):
            self.send_json({"jsonrpc": "2.0", "id": None,
                            "error": {"code": -32700, "message": "Parse error"}}, 400)
            return

        if body is None:
            self.send_json({"jsonrpc": "2.0", "id": None,
                            "error": {"code": -32600, "message": "Empty request"}}, 400)
            return

        if isinstance(body, list):
            responses = [r for r in (handle_rpc(m) for m in body) if r is not None]
            if responses:
                self.send_json(responses)
            else:
                self.send_empty(202)
            return

        response = handle_rpc(body)
        if response is None:
            self.send_empty(202)
        else:
            self.send_json(response)
