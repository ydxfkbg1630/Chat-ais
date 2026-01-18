# Chat Bridge

Bridge ChatGPT and Gemini by relaying replies between two browser tabs.
This project uses Playwright to automate the UI, so you do not need paid APIs.

## Features

- Automatic relay between ChatGPT and Gemini.
- Waits for replies to finish before forwarding.
- Local dashboard that shows both sides in real time.
- Uses a real Chrome session via CDP for better login reliability.

## Requirements

- Windows
- Python 3.9+
- Google Chrome
- Playwright

## Install

```powershell
python -m pip install -r requirements.txt
python -m playwright install
```

## Run

1. Close all Chrome windows.
2. Start Chrome with remote debugging:

```powershell
& "C:\Program Files\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9222 --user-data-dir=".\chrome_profile"
```

3. Log in to ChatGPT and Gemini in that Chrome window.
4. Run the bridge:

```powershell
python chat_bridge.py
```

5. Open the dashboard:

```
http://127.0.0.1:8765
```

## Config

Edit `bridge_config.json` to adjust:

- `max_wait_seconds` for long answers.
- `busy_selectors` and `assistant_selectors` if the UI changes.
- `dashboard` settings.

## Notes

This is UI automation. If the sites change their markup, update the selectors in
`bridge_config.json`.
