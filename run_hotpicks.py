from __future__ import annotations

from typing import Any, Dict, List

from app.config_loader import load_config
from app.alpaca_api import AlpacaClient
from app.hotpicks import compute_hotpicks_for_universe


def _get_universe(config: Dict[str, Any]) -> List[str]:
    """
    Universe priority:
      1) config.hotpicks.universe (if present)
      2) config.risk.allowed_symbols
      3) config.strategy.targets keys
      4) fallback default
    """
    hp = config.get("hotpicks", {}) if isinstance(config.get("hotpicks", {}), dict) else {}
    universe = hp.get("universe")

    if isinstance(universe, list) and universe:
        return [str(x).strip().upper() for x in universe if str(x).strip()]

    risk = config.get("risk", {}) if isinstance(config.get("risk", {}), dict) else {}
    allowed = risk.get("allowed_symbols")
    if isinstance(allowed, list) and allowed:
        return [str(x).strip().upper() for x in allowed if str(x).strip()]

    strat = config.get("strategy", {}) if isinstance(config.get("strategy", {}), dict) else {}
    targets = strat.get("targets")
    if isinstance(targets, dict) and targets:
        return [str(k).strip().upper() for k in targets.keys() if str(k).strip()]

    return ["SPY", "QQQ", "IWM", "AAPL", "MSFT"]


def main() -> int:
    config = load_config()

    # how many daily bars to fetch (default 60)
    hp_cfg = config.get("hotpicks", {}) if isinstance(config.get("hotpicks", {}), dict) else {}
    limit = int(hp_cfg.get("lookback_bars", 60))
    top_n = int(hp_cfg.get("top_n", 5))

    symbols = _get_universe(config)

    print(">>> Hot Picks (Top 5) based on recent momentum/trend/volatility")
    alpaca = AlpacaClient(config)

    picks = compute_hotpicks_for_universe(alpaca, symbols=symbols, limit=limit, top_n=top_n)

    if not picks:
        print("No hot picks could be generated. Check universe config or Alpaca data access.")
        return 1

    # Print a clean table
    print()
    print(f"{'Symbol':<8} {'Score':>10} {'Last':>10} {'20dMom%':>10} {'20dVol%':>10} {'Bars':>6}  Reason")
    print("-" * 110)

    for p in picks:
        print(
            f"{p.symbol:<8} "
            f"{p.score:>10.4f} "
            f"{p.last:>10.2f} "
            f"{p.mom_20d_pct:>10.2f} "
            f"{p.vol_20d_pct:>10.2f} "
            f"{p.bars:>6}  "
            f"{p.reason}"
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
