from __future__ import annotations

import csv
import os
import pathlib
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from flask import Flask, jsonify, request
from flask_cors import CORS

try:
    # Python 3.9+
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None  # type: ignore


# ---- Base directory (stable paths) ----
BASE_DIR = pathlib.Path(__file__).resolve().parents[1]  # ...\synthara
sys.path.insert(0, str(BASE_DIR))  # allows running as script OR module

from app.config_loader import load_config, save_config  # noqa: E402


API_KEY = "jenwb0304071223xyzabc123"

LOG_DIR = BASE_DIR / "logs"
SCHED_LOG = LOG_DIR / "scheduler_trading.log"
CONFIG_YAML = BASE_DIR / "config.yaml"
TRADE_LOG_CSV = BASE_DIR / "trade_log.csv"

DEFAULT_TZ = "America/Indiana/Indianapolis"

app = Flask(__name__)
CORS(app)


def _require_api_key() -> Any:
    api_key = request.headers.get("X-API-Key")
    if api_key != API_KEY:
        return jsonify({"error": "unauthorized"}), 401
    return None


@app.get("/health")
def health():
    return jsonify({"status": "ok"}), 200


@app.get("/latest-log")
def latest_log():
    log_path = str(SCHED_LOG)
    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        tail = lines[-200:] if len(lines) > 200 else lines
        return jsonify({"path": log_path, "lines": tail}), 200
    except FileNotFoundError:
        return jsonify({"error": "log file not found", "path": log_path}), 404
    except Exception as e:
        return jsonify({"error": str(e), "path": log_path}), 500


@app.get("/last-run")
def last_run():
    log_path = str(SCHED_LOG)
    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()

        start_idx = None
        for i in range(len(lines) - 1, -1, -1):
            if lines[i].startswith("==== SCHEDULER START"):
                start_idx = i
                break

        if start_idx is None:
            return jsonify({"error": "no runs found in log", "path": log_path}), 404

        run_block = lines[start_idx:]
        start_line = run_block[0].strip()

        exit_code = None
        for line in reversed(run_block):
            if line.startswith("EXIT_CODE="):
                try:
                    exit_code = int(line.strip().split("=", 1)[1])
                except Exception:
                    exit_code = None
                break

        paper_orders_submitted = any("Paper orders submitted to Alpaca." in l for l in run_block)
        risk_rejects = [l.strip() for l in run_block if "[RiskManager] Rejecting" in l]
        orders_logged = any(("Logged" in l) and ("orders to trade_log.csv" in l) for l in run_block)

        return (
            jsonify(
                {
                    "path": log_path,
                    "start_line": start_line,
                    "exit_code": exit_code,
                    "orders_logged": orders_logged,
                    "paper_orders_submitted": paper_orders_submitted,
                    "risk_reject_count": len(risk_rejects),
                    "risk_rejects": risk_rejects[:10],
                }
            ),
            200,
        )

    except FileNotFoundError:
        return jsonify({"error": "log file not found", "path": log_path}), 404
    except Exception as e:
        return jsonify({"error": str(e), "path": log_path}), 500


@app.get("/status")
def status():
    import re

    config = load_config(CONFIG_YAML)
    kill_switch = bool(config.get("safety", {}).get("kill_switch", False))
    read_only = bool(config.get("safety", {}).get("read_only", False))
    execution_mode = str(config.get("execution", {}).get("mode", "unknown"))

    log_path = str(SCHED_LOG)

    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()

        start_idx = None
        for i in range(len(lines) - 1, -1, -1):
            if lines[i].startswith("==== SCHEDULER START"):
                start_idx = i
                break

        if start_idx is None:
            return jsonify({"error": "no runs found in log", "path": log_path}), 404

        run_block = lines[start_idx:]
        start_line = run_block[0].strip()

        exit_code = None
        for line in reversed(run_block):
            if line.startswith("EXIT_CODE="):
                try:
                    exit_code = int(line.strip().split("=", 1)[1])
                except Exception:
                    exit_code = None
                break

        equity_value = None
        equity_re = re.compile(r"^\s*Equity:\s*\$?([0-9,]+\.[0-9]{2})\s*$")
        for line in reversed(run_block):
            m = equity_re.match(line.strip())
            if m:
                try:
                    equity_value = float(m.group(1).replace(",", ""))
                except Exception:
                    equity_value = None
                break

        orders_logged = any(("Logged" in l) and ("orders to trade_log.csv" in l) for l in run_block)
        paper_orders_submitted = any("Paper orders submitted to Alpaca." in l for l in run_block)
        risk_rejects = [l.strip() for l in run_block if "[RiskManager] Rejecting" in l]

        generated_orders = [l for l in run_block if l.strip().startswith(("BUY ", "SELL "))]
        submitted_paper_orders = [l for l in run_block if l.strip().startswith("[PAPER] Submitting ")]

        return (
            jsonify(
                {
                    "path": log_path,
                    "last_run_start": start_line,
                    "kill_switch": kill_switch,
                    "exit_code": exit_code,
                    "equity": equity_value,
                    "orders_logged": orders_logged,
                    "paper_orders_submitted": paper_orders_submitted,
                    "generated_order_count": len(generated_orders),
                    "paper_submitted_order_count": len(submitted_paper_orders),
                    "risk_reject_count": len(risk_rejects),
                    "risk_rejects": risk_rejects[:10],
                    "read_only": read_only,
                    "execution_mode": execution_mode,
                }
            ),
            200,
        )

    except FileNotFoundError:
        return jsonify({"error": "log file not found", "path": log_path}), 404
    except Exception as e:
        return jsonify({"error": str(e), "path": log_path}), 500


@app.post("/set-safety")
def set_safety():
    auth = _require_api_key()
    if auth is not None:
        return auth

    config = load_config(CONFIG_YAML)
    safety = config.get("safety", {}) or {}

    payload = request.get_json(silent=True) or {}

    if "kill_switch" in payload:
        safety["kill_switch"] = bool(payload["kill_switch"])

    if "read_only" in payload:
        safety["read_only"] = bool(payload["read_only"])

    config["safety"] = safety
    save_config(config, CONFIG_YAML)

    return (
        jsonify(
            {
                "ok": True,
                "safety": {
                    "kill_switch": bool(safety.get("kill_switch", False)),
                    "read_only": bool(safety.get("read_only", False)),
                },
            }
        ),
        200,
    )


@app.post("/set-execution-mode")
def set_execution_mode():
    auth = _require_api_key()
    if auth is not None:
        return auth

    payload = request.get_json(silent=True) or {}
    mode = str(payload.get("mode", "")).strip().lower()

    if mode in ("manual", ""):
        return jsonify({"error": "manual is UI-only; use dry_run/paper/live/none"}), 400

    if mode not in ("dry_run", "paper", "live", "none"):
        return jsonify({"error": "mode must be one of: dry_run, paper, live, none"}), 400

    config = load_config(CONFIG_YAML)
    execution = config.get("execution", {}) or {}
    execution["mode"] = mode
    config["execution"] = execution

    save_config(config, CONFIG_YAML)
    return jsonify({"ok": True, "mode": mode}), 200

@app.get("/settings")
def get_settings():
    auth = _require_api_key()
    if auth is not None:
        return auth

    config = load_config(CONFIG_YAML)
    safety = config.get("safety", {}) or {}
    execution = config.get("execution", {}) or {}

    live = _is_live_armed(config)
    def _num(v):
        try:
            return float(v) if v is not None else None
        except Exception:
            return None

    exceed_cap_behavior = str(safety.get("exceed_cap_behavior", "trim")).strip().lower()
    if exceed_cap_behavior not in ("trim", "reject", "require_approval"):
        exceed_cap_behavior = "trim"

    return (
        jsonify(
            {
                "execution": {"mode": str(execution.get("mode", "paper"))},
                "safety": {
                    "kill_switch": bool(safety.get("kill_switch", False)),
                    "read_only": bool(safety.get("read_only", False)),
                    "cap_min_usd": _num(safety.get("cap_min_usd", 20)),
                    "cap_max_usd": _num(safety.get("cap_max_usd", 500)),
                    "max_order_notional_usd": _num(safety.get("max_order_notional_usd", None)),
                    "exceed_cap_behavior": exceed_cap_behavior,
                    "live_arming": live
                }
            }
        ),
        200,
    )


@app.put("/settings")
def put_settings():
    auth = _require_api_key()
    if auth is not None:
        return auth

    payload = request.get_json(silent=True) or {}
    safety_in = payload.get("safety") or {}

    config = load_config(CONFIG_YAML)
    safety = config.get("safety", {}) or {}

    # ---- Dollar cap bounds (defaults) ----
    cap_min = _to_float(safety.get("cap_min_usd", 20)) or 20.0
    cap_max = _to_float(safety.get("cap_max_usd", 500)) or 500.0



    # max_order_notional_usd is the key control
    if "max_order_notional_usd" in safety_in:
        v = safety_in.get("max_order_notional_usd")
        if v in ("", None):
            safety.pop("max_order_notional_usd", None)
        else:
            try:
                f = float(v)
                if f <= 0:
                    return jsonify({"error": "max_order_notional_usd must be > 0"}), 400
                safety["max_order_notional_usd"] = float(f)
            except Exception:
                return jsonify({"error": "max_order_notional_usd must be a number"}), 400

    # exceed-cap behavior (default to trim)
    if "exceed_cap_behavior" in safety_in:
        beh = str(safety_in.get("exceed_cap_behavior") or "").strip().lower()
        if beh not in ("trim", "reject", "require_approval"):
            return jsonify({"error": "exceed_cap_behavior must be one of: trim, reject, require_approval"}), 400
        safety["exceed_cap_behavior"] = beh
    else:
        safety.setdefault("exceed_cap_behavior", "trim")

    config["safety"] = safety
    save_config(config, CONFIG_YAML)

    return jsonify({"ok": True, "safety": safety}), 200

@app.post("/arm-live")
def arm_live():
    auth = _require_api_key()
    if auth is not None:
        return auth

    payload = request.get_json(silent=True) or {}
    minutes = payload.get("minutes", 15)

    try:
        minutes = int(minutes)
    except Exception:
        return jsonify({"error": "minutes must be an integer"}), 400

    if minutes < 1 or minutes > 120:
        return jsonify({"error": "minutes must be between 1 and 120"}), 400

    config = load_config(CONFIG_YAML)
    execution = config.get("execution", {}) or {}

    until_dt = datetime.now(timezone.utc) + timedelta(minutes=minutes)
    execution["live_armed_until_utc"] = until_dt.isoformat(timespec="seconds").replace("+00:00", "Z")

    config["execution"] = execution
    save_config(config, CONFIG_YAML)

    return jsonify({"ok": True, "live_arming": _is_live_armed(config)}), 200


@app.post("/disarm-live")
def disarm_live():
    auth = _require_api_key()
    if auth is not None:
        return auth

    config = load_config(CONFIG_YAML)
    execution = config.get("execution", {}) or {}
    execution["live_armed_until_utc"] = None
    config["execution"] = execution
    save_config(config, CONFIG_YAML)

    return jsonify({"ok": True, "live_arming": _is_live_armed(config)}), 200


def _parse_ts(ts: str) -> datetime | None:
    if not ts:
        return None
    s = str(ts).strip()
    try:
        # Handle Z suffix if present
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def _to_float(v):
    try:
        return float(v)
    except Exception:
        return None

def _parse_iso_utc(s: str) -> Optional[datetime]:
    if not s:
        return None
    raw = str(s).strip()
    try:
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def _is_live_armed(config: Dict[str, Any]) -> Dict[str, Any]:
    """
    Returns:
      { armed: bool, armed_until_utc: str|None, remaining_seconds: int }
    """
    execution = config.get("execution", {}) or {}
    until_raw = execution.get("live_armed_until_utc", None)
    until_dt = _parse_iso_utc(until_raw) if isinstance(until_raw, str) else None

    now = datetime.now(timezone.utc)
    if until_dt is None:
        return {"armed": False, "armed_until_utc": None, "remaining_seconds": 0}

    remaining = int((until_dt - now).total_seconds())
    if remaining <= 0:
        return {"armed": False, "armed_until_utc": until_dt.isoformat().replace("+00:00", "Z"), "remaining_seconds": 0}

    return {"armed": True, "armed_until_utc": until_dt.isoformat().replace("+00:00", "Z"), "remaining_seconds": remaining}


@app.get("/trade-history")
def trade_history():
    rng = str(request.args.get("range", "24h")).strip().lower()

    now = datetime.now(timezone.utc)

    if rng == "24h":
        cutoff = now - timedelta(hours=24)
    elif rng == "1w":
        cutoff = now - timedelta(days=7)
    elif rng == "1m":
        cutoff = now - timedelta(days=30)
    elif rng == "6m":
        cutoff = now - timedelta(days=182)
    else:
        return jsonify({"error": "range must be one of: 24h, 1w, 1m, 6m"}), 400

    path = str(TRADE_LOG_CSV)

    if not TRADE_LOG_CSV.exists():
        return jsonify({"error": "trade log not found", "path": path}), 404

    rows: List[Dict[str, Any]] = []
    try:
        with TRADE_LOG_CSV.open("r", encoding="utf-8", errors="replace", newline="") as f:
            reader = csv.DictReader(f)
            for r in reader:
                ts = _parse_ts(r.get("timestamp_utc", ""))
                if ts is None:
                    continue
                if ts < cutoff:
                    continue

                # Normalize types for frontend charting
                try:
                    qty = float(r.get("quantity", "0") or 0)
                except Exception:
                    qty = 0.0
                try:
                    px = float(r.get("price", "0") or 0)
                except Exception:
                    px = 0.0

                rows.append(
                    {
                        "timestamp_utc": ts.isoformat().replace("+00:00", "Z"),
                        "mode": r.get("mode", ""),
                        "symbol": (r.get("symbol", "") or "").strip().upper(),
                        "side": (r.get("side", "") or "").strip().lower(),
                        "quantity": qty,
                        "price": px,
                        "reason": r.get("reason", "") or "",
                    }
                )

        return jsonify({"count": len(rows), "path": path, "range": rng, "rows": rows}), 200

    except Exception as e:
        return jsonify({"error": str(e), "path": path}), 500


# ============================================================
# NEW: Schedule endpoint for frontend "Next Scheduled Run" panel
# ============================================================

def _tzinfo():
    if ZoneInfo is None:
        return None
    try:
        return ZoneInfo(DEFAULT_TZ)
    except Exception:
        return None


def _fmt_local(dt_utc: datetime) -> str:
    tz = _tzinfo()
    if tz is None:
        # fallback: ISO in UTC
        return dt_utc.replace(tzinfo=timezone.utc).isoformat(timespec="seconds")
    return dt_utc.astimezone(tz).isoformat(timespec="seconds")


def _try_parse_schtasks_datetime(s: str) -> Optional[datetime]:
    """
    schtasks returns Next Run Time typically like:
      - "1/23/2026 7:00:00 AM"
      - or "N/A"
    We parse it as *local time* (DEFAULT_TZ) and convert to UTC.
    """
    raw = (s or "").strip()
    if not raw or raw.upper() == "N/A":
        return None

    tz = _tzinfo()
    # If ZoneInfo not available, assume local == UTC (best-effort)
    local_tz = tz or timezone.utc

    # Try common Windows formats
    fmts = [
        "%m/%d/%Y %I:%M:%S %p",
        "%m/%d/%Y %H:%M:%S",
    ]
    for fmt in fmts:
        try:
            dt_local = datetime.strptime(raw, fmt)
            dt_local = dt_local.replace(tzinfo=local_tz)
            return dt_local.astimezone(timezone.utc)
        except Exception:
            continue

    return None


def _read_env_task_list() -> List[str]:
    """
    Explicit task list (recommended):
      SYN_SCHEDULE_TASKS="TaskA;TaskB;TaskC"
    """
    raw = str(os.getenv("SYN_SCHEDULE_TASKS", "")).strip()
    if not raw:
        return []
    parts = [p.strip() for p in raw.split(";")]
    return [p for p in parts if p]




def _wildcard_enabled() -> bool:
    return str(os.getenv("SYN_SCHEDULE_WILDCARD", "")).strip() == "1"


def _wildcard_contains() -> str:
    """
    Which substring to match in task names when wildcard is enabled.
    Defaults to "wealth autopilot" to match your Task Scheduler names.
    """
    return (os.getenv("SYN_SCHEDULE_MATCH", "") or "wealth autopilot").strip().lower()



def _query_schtasks_csv() -> List[Dict[str, str]]:
    """
    Query Task Scheduler as CSV so we can parse reliably.
    """
    cmd = ["schtasks", "/Query", "/FO", "CSV", "/V"]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "schtasks query failed")

    text = proc.stdout or ""
    # Sometimes Windows emits a UTF-8 BOM; strip it safely.
    text = text.lstrip("\ufeff")

    # Parse CSV
    rows: List[Dict[str, str]] = []
    reader = csv.DictReader(text.splitlines())
    for r in reader:
        # Normalize keys to stable access
        row = {str(k).strip(): (str(v).strip() if v is not None else "") for k, v in r.items()}
        rows.append(row)
    return rows

def _norm_task_name(name: str) -> str:
    """
    Normalize Task Scheduler names for matching:
    - strip whitespace
    - remove leading backslashes (Task Scheduler often returns '\\TaskName')
    - compare case-insensitively by returning lowercase
    """
    s = (name or "").strip()
    while s.startswith("\\"):
        s = s[1:]
    return s.strip().lower()



def _build_schedule_payload() -> Dict[str, Any]:
    tz = _tzinfo()
    tz_name = DEFAULT_TZ

    explicit = _read_env_task_list()
    wildcard = _wildcard_enabled()
    match_sub = _wildcard_contains()

    discovered_tasks: List[Dict[str, Any]] = []

    rows = _query_schtasks_csv()

    # CSV headers vary slightly by locale; these are the common English headers.
    # If your Windows locale differs, we can adapt once you paste a sample /schedule response.
    TASKNAME_KEY = "TaskName"
    NEXT_KEY = "Next Run Time"
    RUN_KEY = "Task To Run"
    STATUS_KEY = "Status"

    for r in rows:
        task_name_raw = (r.get(TASKNAME_KEY, "") or "").strip()
        if not task_name_raw:
            continue

        task_name_norm = _norm_task_name(task_name_raw)

        # Normalize explicit list once (case-insensitive, strips leading '\')
        explicit_norm = [_norm_task_name(x) for x in explicit] if explicit else []

        include = False
        if explicit_norm:
            include = task_name_norm in explicit_norm
        elif wildcard:
            include = match_sub in task_name_norm
        else:
            # default wildcard matching if neither explicit nor wildcard env set
            include = match_sub in task_name_norm

        if not include:
            continue

        next_run_raw = r.get(NEXT_KEY, "") or ""
        next_run_utc = _try_parse_schtasks_datetime(next_run_raw)

        task_to_run = (r.get(RUN_KEY, "") or "").strip()
        status = (r.get(STATUS_KEY, "") or "").strip()

        discovered_tasks.append(
            {
                "name": task_name_raw,
                "description": task_to_run or status or "",
                "status": status,
                "next_run_iso": next_run_utc.isoformat(timespec="seconds").replace("+00:00", "Z") if next_run_utc else None,
                "next_run_local": _fmt_local(next_run_utc) if next_run_utc else None,
                "will_run": False,  # fill below
            }
        )

    # Determine global next run across tasks
    next_dt: Optional[datetime] = None
    for t in discovered_tasks:
        iso = t.get("next_run_iso")
        dt = _parse_ts(iso) if isinstance(iso, str) else None
        if dt is None:
            continue
        if next_dt is None or dt < next_dt:
            next_dt = dt

    # Mark tasks that match the global next run (same minute)
    if next_dt is not None:
        for t in discovered_tasks:
            iso = t.get("next_run_iso")
            dt = _parse_ts(iso) if isinstance(iso, str) else None
            if dt is None:
                continue
            # same minute is good enough (Task Scheduler precision)
            t["will_run"] = abs((dt - next_dt).total_seconds()) < 60

    payload: Dict[str, Any] = {
        "timezone": tz_name,
        "wildcard_enabled": bool(wildcard) or (not explicit),
        "match": match_sub if (bool(wildcard) or (not explicit)) else None,
        "explicit_tasks": explicit if explicit else None,
        "next_run_iso": next_dt.isoformat(timespec="seconds").replace("+00:00", "Z") if next_dt else None,
        "next_run_local": _fmt_local(next_dt) if next_dt else None,
        "tasks": discovered_tasks,
        "notes": None,
    }

    if not discovered_tasks:
        payload["notes"] = (
            "No scheduled tasks matched. "
            "Set SYN_SCHEDULE_TASKS to explicit task names (semicolon-separated), "
            "or set SYN_SCHEDULE_WILDCARD=1 (optional SYN_SCHEDULE_MATCH=<substring>)."
        )

    if tz is None:
        payload["notes"] = (payload["notes"] or "") + " ZoneInfo unavailable; local time may be shown as UTC."

    return payload


@app.get("/schedule")
def schedule():
    """
    Returns next scheduled run time and task list (Windows Task Scheduler).

    Controls:
      - SYN_SCHEDULE_TASKS="TaskA;TaskB"
      - SYN_SCHEDULE_WILDCARD=1
      - SYN_SCHEDULE_MATCH="wealth autopilot"  (default)
    """
    try:
        payload = _build_schedule_payload()
        return jsonify(payload), 200
    except FileNotFoundError:
        return jsonify({"error": "schtasks not found on this system"}), 500
    except subprocess.TimeoutExpired:
        return jsonify({"error": "schtasks query timed out"}), 504
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/run-now", methods=["POST"])
def run_now():
    auth = _require_api_key()
    if auth is not None:
        return auth

    config = load_config(CONFIG_YAML)
    read_only = bool(config.get("safety", {}).get("read_only", False))
    if read_only:
        return jsonify({"error": "read-only mode enabled"}), 403

    payload = request.get_json(silent=True) or {}

    # Accept BOTH formats:
    # - preferred: { "manual": { "max_usd_per_trade": 10.0 } }
    # - legacy:   { "max_trade_usd": 10.0, "ui_mode": "manual" }
    manual = payload.get("manual") or {}
    max_usd_per_trade = manual.get("max_usd_per_trade", None)

    if max_usd_per_trade is None and "max_trade_usd" in payload:
        max_usd_per_trade = payload.get("max_trade_usd")

    manual_env: Dict[str, str] = {}
    if max_usd_per_trade is not None:
        try:
            cap = float(max_usd_per_trade)
            if cap <= 0:
                return jsonify({"error": "manual max_usd_per_trade must be > 0"}), 400
            manual_env["SYN_MANUAL_MAX_USD_PER_TRADE"] = f"{cap:.4f}"
            manual_env["SYN_MANUAL_ENABLED"] = "1"
        except Exception:
            return jsonify({"error": "manual max_usd_per_trade must be a number"}), 400

    cmd = [
        str(BASE_DIR / ".venv" / "Scripts" / "python.exe"),
        str(BASE_DIR / "run_trading_once.py"),
    ]

    env = os.environ.copy()
    env.update(manual_env)

    try:
        proc = subprocess.run(
            cmd,
            cwd=str(BASE_DIR),
            capture_output=True,
            text=True,
            timeout=180,
            env=env,
        )

        return (
            jsonify(
                {
                    "exit_code": proc.returncode,
                    "stdout": (proc.stdout or "")[-8000:],
                    "stderr": (proc.stderr or "")[-8000:],
                    "manual_applied": bool(manual_env),
                    "manual": {"max_usd_per_trade": float(max_usd_per_trade)} if max_usd_per_trade is not None else None,
                }
            ),
            200,
        )

    except subprocess.TimeoutExpired:
        return jsonify({"error": "run-now timed out"}), 504
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    app.run(host="127.0.0.1", port=5056, debug=False)
