# Run paper trading on Windows

Use Windows Task Scheduler on the machine that will remain powered on and
connected to the internet. The Python cycle runs once every 15 minutes, saves
state to the existing Neon database, then exits. No paid hosting is needed.

## 1. Install the project

Install Python 3.11 and Git for Windows from their official sites. Download or
clone this repository on the Lenovo. Do not copy the Mac `.venv`: virtual
environments must be created on the target operating system.

Open Command Prompt in the repository folder (the one containing `app` and
`requirements.txt`) and run:

```bat
py -3.11 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
copy .env.example .env
```

Only run the last command when `.env` does not already exist. Edit `.env`
locally, preserving its other strategy settings, and set:

```dotenv
DATABASE_URL=YOUR_EXISTING_NEON_CONNECTION_STRING
PORTFOLIO_ACCOUNT_ID=default
PAPER_TRADING=true
LIVE_TRADING_ENABLED=false
LIVE_ENTRIES_ENABLED=false
LLM_ADVISOR_ENABLED=false
TRADING_TIMEZONE=Asia/Kolkata
REQUIRE_PERSISTENT_DATABASE=true
```

Replace the placeholder with the exact URL used by the existing GitHub runs.
Do not share it, put it into task arguments, or commit `.env`. Keeping that
database and account label preserves the positions and evaluation baseline.
No Binance API credentials are required for paper trading.

The `tzdata` dependency supplies IANA time zones on Windows. SQLite file locking
is also Windows-compatible, although this launcher requires PostgreSQL so it
cannot accidentally create a fresh local portfolio.

## 2. Test once

From the repository folder:

```bat
scripts\run-paper-windows.cmd
```

Open `logs\paper-scheduler.log`. A successful run should restore your portfolio,
log `PAPER EVALUATION`, and finish with `exit=0`. Inspect `restored_state=True`
when continuing the existing portfolio. If it is false unexpectedly, check the
database URL and account label before proceeding. Logs rotate at about 10 MB,
keeping one previous file.

## 3. Register the recurring task

Open **Task Scheduler → Create Task** (not Create Basic Task):

- **General:** name `Trado Paper Trading`. Choose **Run whether user is logged
  on or not**. Use your regular Windows account; administrator privileges are
  not needed for the Python job. Windows may request the account password,
  which is different from a Windows Hello PIN. Leave the option that prevents
  storing the password/network access unchecked, because Neon needs a network
  connection.
- **Triggers → New:** choose **Daily**, start at your preferred next quarter
  hour, repeat every **15 minutes**, for **Indefinitely**, and keep it enabled.
- **Actions → New → Start a program:**
  - Program: `C:\Windows\System32\cmd.exe`
  - Arguments: `/d /s /c ""C:\Trado_Backend\scripts\run-paper-windows.cmd""`
  - Start in: `C:\Trado_Backend`
  Replace the example repository path in both fields with its real location.
- **Conditions:** do not require the PC to be idle. Enable **Wake the computer
  to run this task**. Keep the laptop plugged in. Leave battery restrictions
  enabled if you want trading to pause when unplugged; uncheck them only if
  you intentionally want tasks to continue on battery.
- **Settings:** enable **Allow task to be run on demand** and **Run task as soon
  as possible after a scheduled start is missed**. For an already running task,
  select **Do not start a new instance**. Set **Stop the task if it runs longer
  than** to **10 minutes** for this paper job.

Keep this machine awake while plugged in; the display may turn off. Closing the
lid can put a laptop to sleep, so leave it open unless its lid action is
configured otherwise. Wake timers depend on Windows/power/firmware settings;
they cannot run a task while the computer is shut down. Internet must remain
available. Sleep, shutdowns, network failures, or Windows restarts can delay
checks; missed tasks are not continuous risk monitoring.

## 4. Verify automatic operation

1. Right-click the task and choose **Run** once.
2. Confirm **Last Run Result = 0x0**, and inspect the log.
3. Confirm **Next Run Time** is correct.
4. Leave it alone through two consecutive scheduled times. Confirm fresh log
   timestamps, successful exits, and an increasing evaluation-cycle count.
5. Reboot once when convenient and confirm the next scheduled run still occurs.

Only Windows should own the recurring schedule. Disable the GitHub scheduled
workflow through **Actions → workflow → … → Disable workflow** if the deployed
copy still has a cron trigger. Do not create the Render job, and stop any Mac
worker or schedule using this portfolio. The database lock prevents overlap,
but multiple schedulers can still execute consecutive extra cycles.

You do not need to keep Command Prompt, VS Code, or GitHub open. The Windows
machine itself must remain available. This setup is for the paper experiment;
the later live monitor is a separate continuously supervised process.

References:
- https://learn.microsoft.com/en-us/windows-server/administration/windows-commands/schtasks-create
- https://learn.microsoft.com/en-us/windows/win32/api/taskschd/nf-taskschd-itasksettings-put_waketorun
- https://docs.python.org/3/library/zoneinfo.html#data-sources
