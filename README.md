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
