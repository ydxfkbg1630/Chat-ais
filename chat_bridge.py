import http.server
import json
import mimetypes
import queue
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional
from urllib.parse import urlparse

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright


CONFIG_PATH = Path(__file__).with_name("bridge_config.json")


@dataclass
class SiteConfig:
    name: str
    url: str
    input_selectors: List[str]
    assistant_selectors: List[str]
    busy_selectors: List[str]
    ignore_line_contains: List[str]
    assistant_exclude_selectors: List[str]


@dataclass
class SiteState:
    config: SiteConfig
    page: object
    input_selector: str
    assistant_selector: Optional[str]
    last_assistant_text: str


def load_config(path: Path) -> dict:
    if not path.exists():
        raise FileNotFoundError(f"Missing config: {path}")
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def normalize_args(value) -> List[str]:
    if isinstance(value, list):
        return [str(item) for item in value if item is not None]
    return []


def now_timestamp() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def build_context(
    playwright,
    mode: str,
    headless: bool,
    browser_args: List[str],
    chrome_channel: Optional[str],
    user_data_dir: str,
    cdp_url: str,
):
    mode = (mode or "playwright").strip().lower()
    if mode == "cdp":
        browser = playwright.chromium.connect_over_cdp(cdp_url)
        if browser.contexts:
            context = browser.contexts[0]
        else:
            context = browser.new_context()
        return browser, context
    if mode == "chrome_persistent":
        channel = chrome_channel or "chrome"
        context = playwright.chromium.launch_persistent_context(
            user_data_dir, headless=headless, channel=channel, args=browser_args
        )
        return context.browser, context
    launch_kwargs = {"headless": headless, "args": browser_args}
    if chrome_channel:
        launch_kwargs["channel"] = chrome_channel
    browser = playwright.chromium.launch(**launch_kwargs)
    context = browser.new_context()
    return browser, context


class DashboardState:
    def __init__(self, max_events: int):
        self.max_events = max_events
        self.events: List[dict] = []
        self.subscribers: List[queue.Queue] = []
        self.lock = threading.Lock()

    def publish(self, event: dict) -> None:
        with self.lock:
            self.events.append(event)
            if len(self.events) > self.max_events:
                self.events = self.events[-self.max_events :]
            for subscriber in list(self.subscribers):
                subscriber.put(event)

    def snapshot(self) -> List[dict]:
        with self.lock:
            return list(self.events)

    def add_subscriber(self) -> queue.Queue:
        subscriber = queue.Queue()
        with self.lock:
            self.subscribers.append(subscriber)
        return subscriber

    def remove_subscriber(self, subscriber: queue.Queue) -> None:
        with self.lock:
            if subscriber in self.subscribers:
                self.subscribers.remove(subscriber)


@dataclass
class DashboardServer:
    state: DashboardState
    server: http.server.ThreadingHTTPServer
    thread: threading.Thread

    def publish(self, event: dict) -> None:
        self.state.publish(event)

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def make_dashboard_handler(state: DashboardState, web_root: Path):
    class DashboardHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path in ("/", "/index.html"):
                self.serve_file(web_root / "index.html")
                return
            if parsed.path == "/events":
                self.serve_events()
                return
            if parsed.path == "/history":
                self.serve_history()
                return
            target = (web_root / parsed.path.lstrip("/")).resolve()
            if not str(target).startswith(str(web_root.resolve())):
                self.send_error(403)
                return
            if target.exists() and target.is_file():
                self.serve_file(target)
                return
            self.send_error(404)

        def serve_file(self, path: Path) -> None:
            content_type, _ = mimetypes.guess_type(str(path))
            if not content_type:
                content_type = "application/octet-stream"
            data = path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def serve_history(self) -> None:
            payload = json.dumps(state.snapshot(), ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def serve_events(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            self.wfile.flush()

            subscriber = state.add_subscriber()
            try:
                while True:
                    try:
                        event = subscriber.get(timeout=15)
                        payload = json.dumps(event, ensure_ascii=False)
                        message = f"data: {payload}\n\n".encode("utf-8")
                        self.wfile.write(message)
                        self.wfile.flush()
                    except queue.Empty:
                        self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                state.remove_subscriber(subscriber)

        def log_message(self, format: str, *args) -> None:
            return

    return DashboardHandler


def start_dashboard_server(dashboard_config: dict) -> Optional[DashboardServer]:
    if not dashboard_config.get("enabled", False):
        return None
    host = str(dashboard_config.get("host", "127.0.0.1"))
    port = int(dashboard_config.get("port", 8765))
    max_events = int(dashboard_config.get("max_events", 200))
    web_root = Path(__file__).with_name("web")
    state = DashboardState(max_events=max_events)
    handler = make_dashboard_handler(state, web_root)
    server = http.server.ThreadingHTTPServer((host, port), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    print(f"Dashboard: http://{host}:{port}")
    return DashboardServer(state=state, server=server, thread=thread)


def wait_for_any_selector(page, selectors: List[str], timeout_ms: int) -> str:
    end_time = time.time() + (timeout_ms / 1000.0)
    last_error = None
    while time.time() < end_time:
        for selector in selectors:
            try:
                page.wait_for_selector(selector, timeout=500)
                return selector
            except PlaywrightTimeoutError as exc:
                last_error = exc
        time.sleep(0.2)
    joined = ", ".join(selectors)
    raise RuntimeError(f"Could not find any selector: {joined}") from last_error


def ensure_site_state(page, site: SiteConfig, timeout_ms: int) -> SiteState:
    input_selector = wait_for_any_selector(page, site.input_selectors, timeout_ms)
    state = SiteState(
        config=site,
        page=page,
        input_selector=input_selector,
        assistant_selector=None,
        last_assistant_text="",
    )
    state.last_assistant_text = get_last_assistant_text(state)
    return state


def pick_assistant_selector(site: SiteState) -> Optional[str]:
    if site.assistant_selector:
        locator = site.page.locator(site.assistant_selector)
        if locator.count() > 0:
            return site.assistant_selector
        site.assistant_selector = None
    for selector in site.config.assistant_selectors:
        locator = site.page.locator(selector)
        if locator.count() > 0:
            site.assistant_selector = selector
            return selector
    return None


def clean_text(text: str, ignore_contains: List[str]) -> str:
    if not text:
        return ""
    lowered_tokens = [token.lower() for token in ignore_contains if token]
    cleaned_lines = []
    for line in text.splitlines():
        if lowered_tokens:
            lowered_line = line.lower()
            if any(token in lowered_line for token in lowered_tokens):
                continue
        cleaned_lines.append(line)
    return "\n".join(cleaned_lines).strip()


def get_last_assistant_text(site: SiteState) -> str:
    selector = pick_assistant_selector(site)
    if not selector:
        return ""
    locator = site.page.locator(selector)
    count = locator.count()
    if count == 0:
        return ""
    for idx in range(count - 1, -1, -1):
        candidate = locator.nth(idx)
        if is_excluded_element(candidate, site.config.assistant_exclude_selectors):
            continue
        text = candidate.inner_text()
        text = clean_text(text, site.config.ignore_line_contains)
        if text:
            return text
    return ""


def is_excluded_element(locator, exclude_selectors: List[str]) -> bool:
    if not exclude_selectors:
        return False
    selector = ", ".join([item for item in exclude_selectors if item])
    if not selector:
        return False
    try:
        return bool(locator.evaluate("(el, sel) => !!el.closest(sel)", selector))
    except Exception:
        return False


def is_busy(site: SiteState) -> bool:
    selectors = site.config.busy_selectors
    if not selectors:
        return False
    for selector in selectors:
        try:
            locator = site.page.locator(selector)
            if locator.count() == 0:
                continue
            if locator.first.is_visible():
                return True
        except PlaywrightTimeoutError:
            continue
        except Exception:
            continue
    return False


def wait_for_new_assistant(
    site: SiteState,
    poll_seconds: float,
    stable_seconds: float,
    max_wait_seconds: int,
) -> str:
    start = time.time()
    last_seen = ""
    last_change = time.time()
    while time.time() - start < max_wait_seconds:
        current = get_last_assistant_text(site)
        if current and current != site.last_assistant_text:
            if current != last_seen:
                last_seen = current
                last_change = time.time()
            elif time.time() - last_change >= stable_seconds and not is_busy(site):
                site.last_assistant_text = current
                return current
        time.sleep(poll_seconds)
    raise TimeoutError(
        f"Timed out waiting for new assistant message on {site.config.name}."
    )


def clear_and_type(page, selector: str, text: str, type_delay_ms: int) -> None:
    locator = page.locator(selector)
    locator.click()
    tag = locator.evaluate("el => el.tagName.toLowerCase()")
    if tag in ("textarea", "input"):
        locator.fill(text)
    else:
        page.keyboard.press("Control+A")
        page.keyboard.press("Backspace")
        page.keyboard.insert_text(text)
    if type_delay_ms > 0:
        time.sleep(type_delay_ms / 1000.0)


def send_message(site: SiteState, text: str, type_delay_ms: int) -> None:
    clear_and_type(site.page, site.input_selector, text, type_delay_ms)
    site.page.keyboard.press("Enter")


def append_transcript(path: Path, speaker: str, text: str, timestamp: Optional[str]) -> None:
    if not timestamp:
        timestamp = now_timestamp()
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"[{timestamp}] {speaker}\n{text}\n\n")


def prompt_start_side(default_side: str) -> str:
    if default_side in ("chatgpt", "gemini"):
        return default_side
    choice = input("Start side (chatgpt/gemini): ").strip().lower()
    if choice not in ("chatgpt", "gemini"):
        raise ValueError("Start side must be chatgpt or gemini.")
    return choice


def print_ready_instructions() -> None:
    print("Log in to both sites in the opened browser.")
    print("After login, press Enter here to continue.")
    input()


def main() -> int:
    config = load_config(CONFIG_PATH)
    headless = bool(config.get("headless", False))
    browser_mode = str(config.get("browser_mode", "playwright")).strip().lower()
    chrome_channel = config.get("chrome_channel") or None
    user_data_dir = str(config.get("user_data_dir", "chrome_profile"))
    cdp_url = str(config.get("cdp_url", "http://127.0.0.1:9222"))
    browser_args = normalize_args(config.get("browser_args", []))
    poll_seconds = float(config.get("poll_seconds", 1.0))
    stable_seconds = float(config.get("stable_seconds", 2.0))
    max_wait_seconds = int(config.get("max_wait_seconds", 240))
    type_delay_ms = int(config.get("type_delay_ms", 0))
    save_transcript = bool(config.get("save_transcript", False))
    transcript_path = Path(config.get("transcript_path", "transcript.txt"))
    max_rounds = int(config.get("max_rounds", 0))
    seed_prompt = config.get("seed_prompt", "")
    dashboard = start_dashboard_server(config.get("dashboard", {}))

    chatgpt_cfg = SiteConfig(
        name="chatgpt",
        url=config["chatgpt"]["url"],
        input_selectors=config["chatgpt"]["input_selectors"],
        assistant_selectors=config["chatgpt"]["assistant_selectors"],
        busy_selectors=normalize_args(config["chatgpt"].get("busy_selectors", [])),
        ignore_line_contains=normalize_args(
            config["chatgpt"].get("ignore_line_contains", [])
        ),
        assistant_exclude_selectors=normalize_args(
            config["chatgpt"].get("assistant_exclude_selectors", [])
        ),
    )
    gemini_cfg = SiteConfig(
        name="gemini",
        url=config["gemini"]["url"],
        input_selectors=config["gemini"]["input_selectors"],
        assistant_selectors=config["gemini"]["assistant_selectors"],
        busy_selectors=normalize_args(config["gemini"].get("busy_selectors", [])),
        ignore_line_contains=normalize_args(
            config["gemini"].get("ignore_line_contains", [])
        ),
        assistant_exclude_selectors=normalize_args(
            config["gemini"].get("assistant_exclude_selectors", [])
        ),
    )

    start_side = prompt_start_side(config.get("start_side", "chatgpt"))

    with sync_playwright() as p:
        browser, context = build_context(
            p,
            browser_mode,
            headless,
            browser_args,
            chrome_channel,
            user_data_dir,
            cdp_url,
        )
        chatgpt_page = context.new_page()
        gemini_page = context.new_page()
        chatgpt_page.goto(chatgpt_cfg.url, wait_until="domcontentloaded")
        gemini_page.goto(gemini_cfg.url, wait_until="domcontentloaded")

        print_ready_instructions()
        if dashboard:
            dashboard.publish(
                {"type": "status", "text": "ready", "timestamp": now_timestamp()}
            )

        chatgpt_state = ensure_site_state(chatgpt_page, chatgpt_cfg, 30000)
        gemini_state = ensure_site_state(gemini_page, gemini_cfg, 30000)

        if save_transcript:
            if not transcript_path.is_absolute():
                transcript_path = Path(__file__).with_name(str(transcript_path))
            transcript_path.write_text("", encoding="utf-8")

        if start_side == "chatgpt":
            current = chatgpt_state
            other = gemini_state
        else:
            current = gemini_state
            other = chatgpt_state

        if seed_prompt:
            send_message(current, seed_prompt, type_delay_ms)
        else:
            print("Send a message manually in the start side, then press Enter here.")
            input()

        round_index = 0
        try:
            while max_rounds == 0 or round_index < max_rounds:
                response = wait_for_new_assistant(
                    current, poll_seconds, stable_seconds, max_wait_seconds
                )
                timestamp = now_timestamp()
                if save_transcript:
                    append_transcript(
                        transcript_path, current.config.name, response, timestamp
                    )
                if dashboard:
                    dashboard.publish(
                        {
                            "type": "message",
                            "site": current.config.name,
                            "text": response,
                            "timestamp": timestamp,
                        }
                    )
                send_message(other, response, type_delay_ms)
                current, other = other, current
                round_index += 1
        except KeyboardInterrupt:
            print("Stopped by user.")
            return 0
        finally:
            if dashboard:
                dashboard.stop()
            if browser_mode == "chrome_persistent":
                context.close()
            elif browser_mode != "cdp":
                browser.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
