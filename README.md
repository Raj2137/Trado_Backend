# Trado Backend

This repo can run either as a long-lived paper-trading worker or as a durable
scheduled task.

## Current trading policy

- The selector prefilters liquid USDT pairs, backtests the strongest 10 live
  candidates using seven days of hourly data, then returns only the best 2
  qualified coins for allocation.
- The decision agent considers only the top two qualified coins. One coin
  receives 100% of available capital; two coins receive a 70% / 30% split.
  Only exchange-fee dust remains idle.
- Positions are checked every minute for stop-loss and profit-lock exits. A
  position exits at a 2% net loss.
- Once a position reaches +2% net profit, it holds a fixed +2% net-profit
  floor and exits on the next monitoring check below it. A sold coin has a
  15-minute cooldown, then can re-enter only if it still qualifies in a fresh
  scan.
- Market scans run independently from the monitoring loop, so slow candle
  downloads do not delay the one-minute exit checks.
- The market ranking refreshes every 15 minutes. A coin needs at least three
  recent simulated entries and a positive average return after estimated
  round-trip fees; otherwise the worker holds cash rather than forcing a trade.
- A 30% loss from the day's opening equity pauses new entries until the next
  calendar day. Existing positions continue to be monitored by their normal
  stop-loss and profit-lock rules.

All values are configurable in `.env`; start with `.env.example`.

## Optional LLM research adviser

The optional adviser runs every 15 minutes, receives the top-10 metrics plus
current portfolio state, and can use web research to return a structured market
summary and `boost` / `neutral` / `avoid` views. It has no Binance credentials,
cannot submit orders, cannot change stop-loss/profit-lock rules, and is disabled
unless `LLM_ADVISOR_ENABLED=true` and `OPENAI_API_KEY` are both set.

The deterministic engine only uses a high-confidence `avoid` to block a new
entry or to exit a position that is already at least 0.5% down. It limits a
`boost` to a small ranking adjustment. API failure, timeout, or malformed
advice is treated as neutral and never blocks the one-minute risk monitor.
Advice also expires after 20 minutes by default, preventing stale research from
influencing the portfolio when the research service is unavailable.

## Scheduled deployment

### 24–48 hour paper evaluation

Use **Trado paper scheduler** for the experiment. The other workflows shown in
GitHub check code or account access; they do not run the trading strategy.
Start one paper run manually, confirm success, then leave its existing
15-minute schedule enabled. No Binance API keys are needed for paper mode.

Each run writes a **Paper trading evaluation** table on the GitHub run Summary
page and appends a snapshot to the database table `paper_performance`. It shows
elapsed hours, starting/current equity, test P&L, the 5% target, current target
status, first observed target crossing, sampled maximum drawdown, cash, open
positions, realized/unrealized P&L, and intervals between observations.

The target is **5% total return over the observation window**, not 5% per day.
A fresh 100 USDT portfolio therefore has a 105 USDT target. Equity includes
cash plus marked open positions minus estimated sale fees; simulated purchase
fees are already paid. It is not the same as completed, realized trading profit.
The existing paper model does not simulate live slippage or account for taxes
and hosting costs.

The evaluation baseline starts on the first run with reporting enabled and
persists inside `portfolio_state.payload.paper_evaluation`. Existing positions
and cash are retained and valued at that first observation. Do not reset the
database or change strategy/capital midway through a comparison. No missing
history is backfilled, and lifetime realized P&L may predate this test window.
The scheduler keeps running after 24 hours, 48 hours, or a target crossing; it
does not automatically liquidate positions or enable real trading. Drawdown is
sampled at cycle times, so losses between checks may be larger.

This local checkout does not contain the separately developed live safeguards;
its `run_cycle --trade-mode live` is therefore blocked. Before real-money
activation, merge/restore and validate the durable live executor, reconcile the
account, verify protective orders on Spot Testnet, and configure a permitted
live host. A positive 1–2 day paper result alone is not evidence of dependable
future profit.

For a scheduler that invokes the bot every 15 minutes, use the one-cycle
command below. It loads saved portfolio state, protects open positions, scans
the current top two candidates, obtains optional LLM research before entries,
saves the resulting state, and exits:

```bash
python -m app.run_cycle --trade-mode paper
```

Local runs default to SQLite at `data/portfolio_state.db`. A cloud scheduler
must set `DATABASE_URL` to a persistent PostgreSQL database; a container's
local filesystem is not reliable storage between scheduled invocations.

Keep the scheduler enabled after the daily-loss circuit breaker trips. The
cycle exits before scans or LLM work while halted, then automatically resets
the guard on the next calendar day.

## Long-lived worker deployment

For a Docker host, copy the environment template and run the worker with
Docker Compose. The included restart policy brings the process back after a
container or host-process failure:

```bash
cp .env.example .env
docker compose up -d --build
```

The Compose command intentionally runs in paper mode. Change to live mode
only after testing, reconciliation, and exchange-order validation are added.

## Railway deploy

1. Push this repo to GitHub.
2. Create a new Railway project from the repo.
3. Let Railway build from the included `Dockerfile`.
4. Set these environment variables:
   - `PAPER_TRADING=true`
   - `PAPER_TRADING_FEE_RATE=0.001`
   - `BINANCE_API_KEY=` optional for paper mode
   - `BINANCE_SECRET_KEY=` optional for paper mode
5. Deploy.

The container starts this worker process:

```bash
python -m app.run_agent --trade-mode paper
```

## Current limitation

The scheduled mode persists the portfolio ledger but does not yet place
exchange-native protective orders. A 15-minute scheduler can only act at each
scheduled check, so real-money trading should use a more frequent monitor or
exchange-native protection.
