# Trado Backend

This repo can run either as a long-lived paper-trading worker or as a durable
scheduled task.

## Current trading policy

- The selector prefilters liquid USDT pairs, backtests the strongest 10 live
  candidates using seven days of hourly data, then returns only the best 2
  qualified coins for allocation.
- The decision agent considers only the top two qualified coins. One coin
  receives 100% of available capital; two coins receive a 50% / 50% split.
  Only exchange-fee dust remains idle.
- Positions are checked every minute for stop-loss and profit-lock exits. A
  position exits at a 2% net loss in the continuous paper worker. The scheduled
  paper command checks only when invoked, typically every 15 minutes.
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
persists inside `portfolio_state_scoped.payload.paper_evaluation`. Existing positions
and cash are retained and valued at that first observation. Do not reset the
database or change strategy/capital midway through a comparison. No missing
history is backfilled, and lifetime realized P&L may predate this test window.
The scheduler keeps running after 24 hours, 48 hours, or a target crossing; it
does not automatically liquidate positions or enable real trading. Drawdown is
sampled at cycle times, so losses between checks may be larger.

The durable live executor and its safeguards are included, with real trading
disabled by default. Before real-money activation, validate the executor,
reconcile the account, verify protective orders on Spot Testnet, and configure
a permitted live host. A positive 1–2 day paper result alone is not evidence
of dependable future profit.

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

The Compose command intentionally runs in paper mode. The legacy `run_agent`
command is paper-only; live execution uses the durable commands below.

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

## Live execution and read-only account check

Live trading is disabled by default. No workflow in this repository enables
live orders: the scheduled workflow stays in paper mode, and the manual
`Binance read-only account check` workflow only reads account data.

The scanner and paper prices use Binance's documented public-data-only service
at `https://data-api.binance.vision`. Live account operations use Binance's
authenticated spot API. Public-data connectivity does not establish that
authenticated trading is available from a particular host or for an account.
Use a permitted location and a fixed outbound IP for a live host; restrict the
API key to that IP, grant only required reading/spot trading permissions, and
leave withdrawals disabled. Do not work around regional account restrictions.

### Environment secrets and variables

In GitHub, open **Settings → Secrets and variables → Actions**. Store credentials
under **Secrets**, and the non-secret limits under **Variables**:

| Name | Type | Purpose |
| --- | --- | --- |
| `DATABASE_URL` | Secret | Persistent PostgreSQL connection, already used by paper mode |
| `BINANCE_API_KEY` | Secret | Production account key; use read-only permissions initially |
| `BINANCE_SECRET_KEY` | Secret | Production API secret |
| `BINANCE_TESTNET_API_KEY` | Secret | Separate Binance Spot Testnet key |
| `BINANCE_TESTNET_SECRET_KEY` | Secret | Separate Spot Testnet secret |
| `PORTFOLIO_ACCOUNT_ID` | Variable | Stable bot/account label; defaults to `default` |
| `LIVE_CAPITAL_LIMIT_USDT` | Variable | Explicit total bot budget; defaults to `0` so live checks/orders fail closed |
| `LIVE_MAX_POSITION_USDT` | Variable | Explicit per-position budget; defaults to `0` |

Do not add Binance credentials to the paper workflow. It does not need them.
The read-only workflow maps the secrets above into its environment. Merely
creating GitHub Variables does not automatically pass them to arbitrary jobs.
For the manual account check, choose the Spot Testnet input; it defaults to
true. Turn it off only when you intend to inspect the production account.
Checks can still fail on GitHub-hosted runners because authenticated Binance
access is location/IP dependent. In that case run the same command on the
intended host rather than loosening API-key restrictions.

Keep the following controls in the deployment environment for an eventual
live host (they are documented in `.env.example`):

| Setting | Default | Behavior |
| --- | --- | --- |
| `BINANCE_TESTNET` | `false` | Uses Testnet credentials and endpoints when true |
| `LIVE_TRADING_ENABLED` | `false` | Master gate for all exchange order submissions/cancellations |
| `LIVE_ENTRIES_ENABLED` | `false` | Entry pause; when false, live monitoring/protective exits still work if the master gate is enabled |
| `LIVE_DAILY_LOSS_LIMIT_PCT` | `0.02` | Daily pause of new live entries, separate from the paper limit |
| `LIVE_FEE_RESERVE_PCT` | `0.005` | Conservative sizing and exit estimate; realized accounting uses confirmed commissions |
| `LIVE_MAX_SPREAD_PCT` | `0.003` | Rejects entries with a missing or excessive bid/ask spread |
| `LIVE_STOP_LIMIT_GAP_PCT` | `0.005` | Distance between native stop trigger and sell limit |
| `LIVE_MONITOR_INTERVAL_SECONDS` | `15` | Monitor cadence on a continuously running host |
| `TRADING_TIMEZONE` | `Asia/Kolkata` | Calendar-day boundary for the daily loss guard |

Set the two budget limits to amounts you explicitly intend the bot to manage.
They are not taken from the paper `$100` balance. On first live initialization,
the bot allocates at most the configured total cap from actual free USDT. It
does not adopt or sell unrelated existing coins, and does not automatically
credit additional deposits to its budget. Use one stable account label and
avoid other processes trading the same bot funds; a dedicated supported
subaccount provides stronger separation than an internal ledger alone.

### Commands and rollout

With credentials, database URL, and explicit limits set in the environment:

```bash
# Does not submit/cancel orders or save a portfolio snapshot.
python -m app.run_cycle --trade-mode reconcile
```

First validate with Spot Testnet (`BINANCE_TESTNET=true` and separate Testnet
keys). After those checks, enabling both live gates allows the command below
to place actual orders in the selected environment. Keep the gates false on
production until exchange testing and host setup are complete.

```bash
# Strategy cycle: reconcile, protect, scan, and optionally enter.
python -m app.run_cycle --trade-mode live

# Reconcile/protect only; no scans or entries.
python -m app.run_cycle --trade-mode live --monitor-only

# Continuous monitor on the live host. Configure the host to restart it.
python -m app.run_live_monitor
```

Run the strategy cycle every 15 minutes on the live host, with the monitor
running independently. Both use the same database lock. A competing cycle is
rejected rather than running concurrently. A monitoring interval is a target,
not a real-time guarantee; network calls and API latency can take longer.
Turning off only `LIVE_ENTRIES_ENABLED` is the normal emergency entry pause.
Turning off the master gate stops the bot's order/cancel operations too, but
does not cancel already-open exchange orders. Inspect/manage those on Binance.

### State, recovery, accounting and protection

- `portfolio_state_scoped` separates paper, production, and Testnet by account
  label. Existing row `portfolio_state.id=1` is imported only for the default
  paper portfolio on its first scoped read; the legacy row is retained.
- Live snapshots bind to a fingerprint of the API key and to the initial cap.
  Changing a key or cap requires an explicit reconciliation/migration; do not
  delete the state to bypass the check while real holdings/orders exist.
- The PostgreSQL transaction advisory lock (SQLite file lock locally) covers
  the whole cycle, including its exchange operations. Database writes verify
  the live lock before order submission.
- `order_journal` records a unique client order ID and intent before submission.
  Portfolio changes and the applied flag commit together. A restart queries
  the same client ID rather than submitting another order.
- Pending, partially filled, or timed-out orders are reconciled. Pending market
  orders block further trading; partial outstanding orders are canceled and
  their confirmed fills applied. Unsold quantities and proportional cost basis
  remain tracked. Missing fill/commission data blocks the cycle.
- An outcome that remains `OrderNotFound` is **not** assumed safe to retry.
  Inspect that journal ID against Binance order/trade history and resolve the
  discrepancy manually. No automatic journal deletion or retry button is
  provided. This can occur even after a definitive API rejection; conservative
  recovery intentionally favors pausing over a duplicate trade.
- The bot uses one authoritative commission representation per trade. Base
  commissions reduce purchased quantity rather than being charged twice in
  cost basis. Third-asset commissions are valued in USDT and charged against
  the internal bot budget; this may leave more actual USDT than the ledger.
- Buys use a bounded quote amount, with fee reserve and market precision/minimum
  checks. Existing symbols or unsupported native-stop markets are rejected.
- A confirmed buy is checkpointed and followed immediately by a journalled
  native `STOP_LOSS_LIMIT` sell for the sellable quantity. The monitor raises
  the stop when the profit lock activates; the old stop is confirmed canceled
  before replacement or discretionary sale. Fills racing with cancellation
  are reconciled before any further sale.

**Execution limitations:** stop-limit orders can remain unfilled during price
gaps; they do not guarantee a 2% loss cap or a 2% realized profit. A buy and its
protective order are separate API requests, and stop replacement has a short
cancel/recreate gap. A crash or rejection during either gap requires recovery;
if protection cannot be installed, the cycle fails and new entries stop until
resolved. Tiny partial-fill residuals below exchange minimums remain in state
and require manual dust management. This implementation uses REST reconciliation,
not a persistent account WebSocket stream.

### Tests and operational alerts

```bash
python -m unittest discover -s tests -v
```

`Backend safety checks` runs the mocked failure/recovery tests on push and pull
requests, without exchange credentials. Workflow failures appear in GitHub's
job summary; enable your GitHub Actions failure notifications. The standalone
monitor logs failures to stderr; configure the host's process-failure/log
alerts before deploying it. No external notification service is configured.

CI also tests locking and atomic journal application against a temporary
PostgreSQL service via `TEST_POSTGRES_URL`. It never uses the deployment
`DATABASE_URL` or Binance credentials for those tests.

Mocked tests do not certify exchange behavior. Actual Testnet fills, API filters,
protective order support, the deployed database connection/pool configuration,
and the intended host's authenticated access still require verification before
production activation.
