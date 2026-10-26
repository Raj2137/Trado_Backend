# Trado Backend

This repo can run as a long-lived paper-trading worker.

## Current trading policy

- The selector ranks the top 10 qualifying USDT spot pairs using seven days
  of hourly data, and backtests the same seven-day window.
- The decision agent opens at most 3 positions and initially deploys 75% of
  configured capital. The remaining 25% is held as a cash reserve.
- Positions are checked every 60 seconds. A position exits at a 2% net loss.
- Once a position reaches +2%, it protects at least +1% profit and exits if
  its net profit drops 2 percentage points from its best observed profit.
- Market scans run independently from the monitoring loop, so slow candle
  downloads do not delay the one-minute exit checks.

All values are configurable in `.env`; start with `.env.example`.

## Optional LLM research adviser

The optional adviser runs every ten minutes, receives the top-10 metrics plus
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

## Local or VM deployment

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

Paper mode is safe to test, but the bot is stateless across restarts. Open
positions, available capital, and realized PnL are only kept in process memory.
If the worker restarts, paper-trading state resets.
