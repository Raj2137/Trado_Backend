"""Persist a forward paper-evaluation window without resetting its portfolio."""

import logging
import os
from datetime import datetime, timezone

logger = logging.getLogger(__name__)
TARGET_RETURN = 0.05


def start_evaluation(agent, previous=None, now=None):
    if previous is not None:
        return dict(previous)
    now = now or datetime.now(timezone.utc)
    equity = agent._get_portfolio_summary()["equity_usdt"]
    if equity <= 0:
        raise ValueError("Paper evaluation needs positive starting equity")
    return {
        "started_at": now.isoformat(), "starting_equity_usdt": equity,
        "peak_equity_usdt": equity, "max_drawdown_pct": 0.0,
        "target_first_reached_at": None, "cycles": 0, "failed_cycles": 0,
        "max_gap_seconds": 0.0,
    }


def record_performance(agent, evaluation, cycle_succeeded, now=None):
    now = now or datetime.now(timezone.utc)
    valuation = agent._get_portfolio_summary()
    equity = valuation["equity_usdt"]
    initial = evaluation["starting_equity_usdt"]
    net_return = (equity - initial) / initial
    evaluation["peak_equity_usdt"] = max(equity, evaluation["peak_equity_usdt"])
    peak = evaluation["peak_equity_usdt"]
    evaluation["max_drawdown_pct"] = max(evaluation["max_drawdown_pct"], (peak - equity) / peak)
    if net_return >= TARGET_RETURN and evaluation["target_first_reached_at"] is None:
        evaluation["target_first_reached_at"] = now.isoformat()
    if evaluation.get("last_recorded_at"):
        gap = (now - datetime.fromisoformat(evaluation["last_recorded_at"])).total_seconds()
        evaluation["max_gap_seconds"] = max(gap, evaluation["max_gap_seconds"])
    evaluation["last_recorded_at"] = now.isoformat()
    evaluation["cycles"] += 1
    evaluation["failed_cycles"] += int(not cycle_succeeded)
    hours = (now - datetime.fromisoformat(evaluation["started_at"])).total_seconds() / 3600
    report = {
        **evaluation,
        "recorded_at": now.isoformat(), "elapsed_hours": hours,
        "equity_usdt": equity, "test_pnl_usdt": equity - initial,
        "test_return_pct": net_return * 100,
        "target_equity_usdt": initial * (1 + TARGET_RETURN),
        "target_met_now": net_return >= TARGET_RETURN,
        "cash_usdt": valuation["available_usdt"],
        "open_positions": valuation["open_positions"],
        "unrealized_pnl_usdt": valuation["net_unrealized_pnl_usdt"],
        "lifetime_realized_pnl_usdt": agent.realized_pnl_usdt,
        "lifetime_closed_trades": agent.total_trades,
        "daily_trading_halted": agent.daily_trading_halted,
        "cycle_succeeded": cycle_succeeded,
        "observation": "48 hours complete" if hours >= 48 else ("24 hours complete; continue to 48" if hours >= 24 else "collecting first 24 hours"),
    }
    logger.info(
        "PAPER EVALUATION | hours=%.2f | equity=%.2f | test pnl=%+.2f USDT (%+.2f%%) | target=%.2f | sampled max drawdown=%.2f%%",
        hours, equity, equity - initial, net_return * 100,
        report["target_equity_usdt"], evaluation["max_drawdown_pct"] * 100,
    )
    return report


def write_github_summary(report):
    path = os.getenv("GITHUB_STEP_SUMMARY")
    if not path:
        return
    reached = report["target_first_reached_at"] or "Not reached"
    rows = [
        ("Started (UTC)", report["started_at"]),
        ("Observation", report["observation"]),
        ("Elapsed", f"{report['elapsed_hours']:.2f} hours"),
        ("Starting marked equity", f"{report['starting_equity_usdt']:.2f} USDT"),
        ("Current marked equity", f"{report['equity_usdt']:.2f} USDT"),
        ("Test profit/loss", f"{report['test_pnl_usdt']:+.2f} USDT ({report['test_return_pct']:+.2f}%)"),
        ("5% total-return target", f"{report['target_equity_usdt']:.2f} USDT"),
        ("At or above target now", "Yes" if report["target_met_now"] else "No"),
        ("First sampled target crossing (UTC)", reached),
        ("Maximum sampled drawdown", f"{report['max_drawdown_pct'] * 100:.2f}%"),
        ("Available cash", f"{report['cash_usdt']:.2f} USDT"),
        ("Open positions", report["open_positions"]),
        ("Current unrealized P&L", f"{report['unrealized_pnl_usdt']:+.2f} USDT"),
        ("Lifetime realized trading P&L", f"{report['lifetime_realized_pnl_usdt']:+.2f} USDT"),
        ("Lifetime closed trades", report["lifetime_closed_trades"]),
        ("Recorded cycles / failed cycles", f"{report['cycles']} / {report['failed_cycles']}"),
        ("Longest interval between reports", f"{report['max_gap_seconds'] / 60:.1f} minutes"),
        ("Daily entry halt", "Active" if report["daily_trading_halted"] else "Inactive"),
        ("This cycle", "Succeeded" if report["cycle_succeeded"] else "Failed"),
    ]
    with open(path, "a", encoding="utf-8") as summary:
        summary.write("## Paper trading evaluation\n\n| Metric | Value |\n| --- | --- |\n")
        for label, value in rows:
            summary.write(f"| {label} | {value} |\n")
        summary.write(
            "\nEquity includes cash plus open holdings valued at current prices, less estimated sell fees. "
            "Simulated buy fees are already deducted. The target is 5% across the whole observation window, not 5% per day. "
            "Reaching it does not close positions or stop the scheduler.\n\n"
            "The window starts when reporting is first enabled, preserves existing positions, and is retained across runs. "
            "Lifetime realized P&L can include trades from before this window. Drawdown is sampled at cycle times; "
            "intra-cycle movements, live slippage, taxes, and hosting costs are not modeled.\n"
        )
