# Trado Backend

This repo can run as a long-lived paper-trading worker.

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
