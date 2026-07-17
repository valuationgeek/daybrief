# Scheduling daybrief

daybrief has no built-in scheduler by design: `python main.py` runs the full pipeline once and exits. To get a digest every morning, schedule that command with your operating system's native scheduler. Recipes below assume a 07:30 daily run and that you cloned into `~/daybrief` (Linux/macOS) or `C:\daybrief` (Windows) — adjust paths to yours.

## Linux — cron

```bash
crontab -e
```

Add:

```cron
30 7 * * * cd $HOME/daybrief && .venv/bin/python main.py >> logs/cron.log 2>&1
```

## Linux — systemd timer (survives better than cron on desktops)

`~/.config/systemd/user/daybrief.service`:

```ini
[Unit]
Description=daybrief daily news digest

[Service]
Type=oneshot
WorkingDirectory=%h/daybrief
ExecStart=%h/daybrief/.venv/bin/python main.py
```

`~/.config/systemd/user/daybrief.timer`:

```ini
[Unit]
Description=Run daybrief every morning

[Timer]
OnCalendar=*-*-* 07:30:00
Persistent=true

[Install]
WantedBy=timers.target
```

Then:

```bash
systemctl --user daemon-reload
systemctl --user enable --now daybrief.timer
systemctl --user list-timers          # verify
```

`Persistent=true` means a missed run (laptop asleep at 07:30) fires as soon as you're back.

## macOS — launchd

`~/Library/LaunchAgents/com.daybrief.daily.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.daybrief.daily</string>
  <key>WorkingDirectory</key><string>/Users/YOU/daybrief</string>
  <key>ProgramArguments</key>
  <array>
    <string>/Users/YOU/daybrief/.venv/bin/python</string>
    <string>main.py</string>
  </array>
  <key>StartCalendarInterval</key>
  <dict>
    <key>Hour</key><integer>7</integer>
    <key>Minute</key><integer>30</integer>
  </dict>
  <key>StandardOutPath</key><string>/Users/YOU/daybrief/logs/launchd.log</string>
  <key>StandardErrorPath</key><string>/Users/YOU/daybrief/logs/launchd.log</string>
</dict>
</plist>
```

```bash
launchctl load ~/Library/LaunchAgents/com.daybrief.daily.plist
```

## Windows — Task Scheduler

One line in an elevated (or normal) PowerShell/Command Prompt:

```
schtasks /create /tn "daybrief" /tr "C:\daybrief\run.bat" /sc daily /st 07:30
```

Or via the GUI: Task Scheduler → Create Basic Task → Daily 07:30 → Start a program → `C:\daybrief\run.bat`. In the task's settings, enable **"Run task as soon as possible after a scheduled start is missed"** so a sleeping PC catches up.

To remove: `schtasks /delete /tn "daybrief"`.

## GitHub Actions (no always-on machine needed)

If you don't have a machine that's on every morning, daybrief can run entirely in the cloud on a schedule. This requires `llm.provider: "openai"` (runners have no Ollama) and email/Telegram outputs (there's no Obsidian vault in the cloud). See the ready-made workflow at [`.github/workflows/digest.yml`](../.github/workflows/digest.yml) — it persists the SQLite database between runs with `actions/cache` so deduplication and trend detection keep working.
