from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Dict

from app.config_loader import load_config
from app.settings import load_app_settings
from app.alpaca_api import AlpacaClient
from app.broker import BrokerAdapter
from app.orders import OrderExecutor
from app.trading_engine import TradingEngine
from app.risk import RiskManager, RiskConfig
from app.strategy_basic import MultiFixedTargetSharesStrategy
from app.strategy_rotation import HotPicksTargetsStrategy


def _get_hotpicks_config(config: Dict[str, Any]) -> Dict[str, Any]:
    hp = config.get("hotpicks", {}) or {}
    universe = hp.get("universe", []) or []
    universe = [str(x).strip().upper() for x in universe if str(x).strip()]
    top_n = int(hp.get("top_n", 5))
    bars_limit_raw = hp.get("bars_limit", 60)

    try:
        bars_limit = int(bars_limit_raw) if bars_limit_raw is not None else 60
    except Exception:
        bars_limit = 60

    return {"universe": universe, "top_n": top_n, "bars_limit": bars_limit}


def _read_manual_cap() -> float | None:
    """
    If SYN_MANUAL_ENABLED=1 and SYN_MANUAL_MAX_USD_PER_TRADE is set,
    return cap float; else None.
    """
    enabled = str(os.getenv("SYN_MANUAL_ENABLED", "")).strip() == "1"
    if not enabled:
        return None

    raw = os.getenv("SYN_MANUAL_MAX_USD_PER_TRADE", "")
    if not raw:
        return None

    try:
        cap = float(raw)
        if cap <= 0:
            return None
        return cap
    except Exception:
        return None

def _parse_iso_utc(s: str):
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


def _live_arming_check(config: Dict[str, Any]) -> tuple[bool, str]:
    """
    Returns (allowed, reason).
    Live trading is allowed only if execution.mode == 'live' AND live_armed_until_utc is in the future.
    """
    mode = str((config.get("execution", {}) or {}).get("mode", "paper")).strip().lower()
    if mode != "live":
        return True, "not live mode"

    execution = config.get("execution", {}) or {}
    until_raw = execution.get("live_armed_until_utc", None)
    until_dt = _parse_iso_utc(until_raw) if isinstance(until_raw, str) else None

    if until_dt is None:
        return False, "LIVE blocked: not armed (live_armed_until_utc missing)."

    now = datetime.now(timezone.utc)
    if until_dt <= now:
        return False, "LIVE blocked: arming expired."

    remaining = int((until_dt - now).total_seconds())
    return True, f"armed ({remaining}s remaining)"



def _to_int(v) -> int | None:
    if v is None:
        return None
    try:
        return int(v)
    except Exception:
        return None


def _to_float(v) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except Exception:
        return None


def main() -> int:
    print(">>> Trading engine demo (dry-run or paper)")

    config = load_config()
    settings = load_app_settings(config)

    allowed, reason = _live_arming_check(config)
    if not allowed:
        print(f"[SAFETY] {reason}")
        print("[SAFETY] Live trading aborted. Use the UI to arm live trading for a limited time.")
        # Exit code 0 = safe abort, no crash; preserves scheduler stability.
        return 0
    else:
        if str(settings.execution.mode).strip().lower() == "live":
            print(f"[SAFETY] Live arming status: {reason}")


    safety = config.get("safety", {}) or {}
    kill_switch = bool(safety.get("kill_switch", False))
    read_only = bool(safety.get("read_only", False))

    broker = BrokerAdapter(config)
    alpaca = AlpacaClient(config)

    executor = OrderExecutor(mode=settings.execution.mode, alpaca_client=alpaca)
    print(f">>> Execution mode from config: {settings.execution.mode}")

    equity_now = broker.get_current_equity()
    print(f"Equity: ${equity_now:,.2f}")
    print(f">>> Using strategy.type = {settings.strategy.type}")

    # ---- Manual cap (run-now only) ----
    manual_cap = _read_manual_cap()
    if manual_cap is not None:
        print(f"[MANUAL] Enabled. max_usd_per_trade=${manual_cap:,.2f}")

    # ---- RiskConfig (do NOT overwrite position caps with manual per-trade cap) ----
    rcfg = RiskConfig(
        allowed_symbols=settings.risk.allowed_symbols,
        max_position_value_per_symbol=settings.risk.max_position_value_per_symbol,
        max_total_portfolio_exposure=settings.risk.max_total_portfolio_exposure,
        long_only=settings.risk.long_only,
        paper_only=settings.risk.paper_only,
    )
    risk_mgr = RiskManager(rcfg)

    # Choose strategy
    if settings.strategy.type == "multi_fixed_targets":
        strategy = MultiFixedTargetSharesStrategy(targets=settings.strategy.targets)

    elif settings.strategy.type == "hotpicks_targets":
        hp = _get_hotpicks_config(config)
        if not hp["universe"]:
            raise ValueError("hotpicks.universe is missing or empty in config.yaml")

        print(
            f">>> HotPicks config: top_n={hp['top_n']}, bars_limit={hp['bars_limit']}, universe_len={len(hp['universe'])}"
        )

        strategy = HotPicksTargetsStrategy(
            alpaca_client=alpaca,
            universe=hp["universe"],
            top_n=hp["top_n"],
            bars_limit=hp["bars_limit"],
            equal_weight=True,
            rebalance_threshold_pct=settings.strategy.rebalance_threshold_pct,
        )

        targets = strategy.compute_targets(equity_now)
        if not targets:
            print(">>> HotPicksTargetsStrategy produced no targets (no picks).")
        else:
            print(">>> HotPicksTargetsStrategy (computed targets)")
            for sym, qty in targets.items():
                print(f"  {sym}: target_qty={qty}")
            strategy.set_cached_targets(targets)

    else:
        raise ValueError(f"Unknown strategy.type: {settings.strategy.type}")

    # ---- Safety caps from config.yaml ----
    max_orders_per_run = _to_int(safety.get("max_orders_per_run", None))
    max_new_exposure_usd_per_run = _to_float(safety.get("max_new_exposure_usd_per_run", None))
    min_order_notional_usd = _to_float(safety.get("min_order_notional_usd", None))

    # Optional config-based per-order cap (if you later add it to config.yaml)
    # Supports either key name:
    #   safety.max_order_notional_usd
    #   safety.max_order_notional_usd_per_order
    config_per_order_cap = _to_float(
        safety.get("max_order_notional_usd", None)
        if safety.get("max_order_notional_usd", None) is not None
        else safety.get("max_order_notional_usd_per_order", None)
    )

    # Manual per-trade cap overrides config per-order cap ONLY for this run
    max_order_notional_usd = manual_cap if manual_cap is not None else config_per_order_cap

    if read_only:
        print("[SAFETY] Read-only is ENABLED. Trading aborted.")
        exit_code = 0
    else:
        # Execute with accurate exit code + audit even on failure
        exit_code = 0
        try:
            engine = TradingEngine(
                broker=broker,
                alpaca=alpaca,
                strategy=strategy,
                executor=executor,
                risk_mgr=risk_mgr,
                market_price_fn=alpaca.get_last_price,
                kill_switch=kill_switch,
                max_orders_per_run=max_orders_per_run,
                max_new_exposure_usd_per_run=max_new_exposure_usd_per_run,
                min_order_notional_usd=min_order_notional_usd,
                max_order_notional_usd=max_order_notional_usd,
            )

            engine.run_once()

        except Exception as e:
            exit_code = 1
            print(f"[ERROR] run_trading_once failed: {e!r}")

    # --- AUDIT RECORD (append-only JSON) ---
    from app.audit import RunAuditRecord, write_run_audit

    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "_run"

    notes = "run_trading_once.py completed"
    if manual_cap is not None:
        notes += f" | MANUAL max_usd_per_trade={manual_cap:.2f}"
    if read_only:
        notes += " | READ_ONLY abort"

    record = RunAuditRecord(
        ts_utc=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        run_id=run_id,
        mode=str(settings.execution.mode),
        kill_switch=bool(safety.get("kill_switch", False)),
        read_only=bool(safety.get("read_only", False)),
        exit_code=int(exit_code),
        equity=float(equity_now) if equity_now is not None else None,
        notes=notes,
    )

    path = write_run_audit(record)
    print(f"[AUDIT] Wrote run audit record: {path}")

    return int(exit_code)


if __name__ == "__main__":
    raise SystemExit(main())
