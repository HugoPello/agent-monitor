"""Run the monitor from the system tray, with no terminal open.

``agent-monitor-tray`` starts the server in a background thread and shows an
icon in the notification area, then opens the dashboard in the browser.
Clicking the icon opens it again; its menu has
"Quit", which stops the server cleanly and removes the icon. If a monitor
already answers on the configured port (a second launch, or the Docker
container), it just opens the dashboard.

The tray itself needs the optional ``tray`` extra (pystray and Pillow):

    pip install -e ".[tray]"

Everything except ``main`` works without a display, so it can be tested.
"""

import argparse
import contextlib
import json
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, TextIO

from agent_monitor.config import Settings
from agent_monitor.server import MonitorServer, build_server

if TYPE_CHECKING:
    from PIL.Image import Image

APP_NAME = "agent-monitor"
START_TIMEOUT_SECONDS = 10
STOP_TIMEOUT_SECONDS = 10
ICON_COLOUR = "#22d3ee"  # "busy" cyan from DESIGN.md
LOG_FILE_NAME = "agent-monitor-tray.log"
OPEN_DEBOUNCE_SECONDS = 1.0  # longer than a double-click


class ServerThread:
    """The monitor server in a background thread, started and stopped on demand.

    Stopping sets uvicorn's ``should_exit`` flag, the same path as Ctrl+C, so
    ``MonitorServer`` still closes the SSE streams first and exits quickly.
    """

    def __init__(self, settings: Settings, **config_overrides: object) -> None:
        self.server: MonitorServer = build_server(settings, **config_overrides)
        self._thread = threading.Thread(target=self._serve, name=APP_NAME, daemon=True)

    def _serve(self) -> None:
        # uvicorn calls sys.exit(1) when it cannot bind, after logging why.
        # Ending the thread quietly lets start() report it.
        with contextlib.suppress(SystemExit):
            self.server.run()

    def start(self, timeout: float = START_TIMEOUT_SECONDS) -> None:
        """Start serving; raise ``RuntimeError`` if the server cannot start."""
        self._thread.start()
        deadline = time.monotonic() + timeout
        while not self.server.started:
            # uvicorn exits its thread when it cannot bind (port in use...).
            if not self._thread.is_alive():
                raise RuntimeError("the server stopped while starting (is the port in use?)")
            if time.monotonic() > deadline:
                raise RuntimeError(f"the server did not start within {timeout:g} s")
            time.sleep(0.05)

    @property
    def port(self) -> int:
        """The port actually bound (differs from the setting when it was 0)."""
        return self.server.servers[0].sockets[0].getsockname()[1]

    @property
    def running(self) -> bool:
        return self._thread.is_alive()

    def stop(self, timeout: float = STOP_TIMEOUT_SECONDS) -> None:
        self.server.should_exit = True
        self._thread.join(timeout)


class Debounce:
    """True on the first call, then False until ``seconds`` have passed.

    pystray runs the default menu item on every left click, so a
    double-click would otherwise open the dashboard twice.
    """

    def __init__(self, seconds: float, clock: Callable[[], float] = time.monotonic) -> None:
        self._seconds = seconds
        self._clock = clock
        self._last: float | None = None

    def __call__(self) -> bool:
        now = self._clock()
        if self._last is not None and now - self._last < self._seconds:
            return False
        self._last = now
        return True


def dashboard_url(host: str, port: int) -> str:
    return f"http://{host}:{port}/"


def already_running(base_url: str, timeout: float = 1.0) -> bool:
    """Whether an agent-monitor already answers at ``base_url``."""
    try:
        with urllib.request.urlopen(f"{base_url}health", timeout=timeout) as response:
            return response.status == 200 and json.loads(response.read()).get("status") == "ok"
    except (urllib.error.URLError, OSError, ValueError):
        return False


def draw_icon(size: int = 64) -> "Image":
    """The brand mark (one agent, two subagents) in cyan on a transparent ground.

    Drawn at 4x and scaled down, which gives smooth edges without an
    anti-aliasing API. The geometry is the 20x20 SVG in index.html.
    """
    from PIL import Image, ImageDraw

    scale = size * 4 / 20
    big = Image.new("RGBA", (size * 4, size * 4), (0, 0, 0, 0))
    draw = ImageDraw.Draw(big)

    def at(x: float, y: float) -> tuple[float, float]:
        return x * scale, y * scale

    def dot(x: float, y: float, r: float, **style: object) -> None:
        draw.ellipse([*at(x - r, y - r), *at(x + r, y + r)], **style)

    line = round(1.8 * scale)  # a little heavier than the SVG: tray icons are tiny
    draw.line([at(7.6, 8.8), at(13, 5.5)], fill=ICON_COLOUR, width=line)
    draw.line([at(7.6, 11.2), at(13, 14.5)], fill=ICON_COLOUR, width=line)
    dot(5, 10, 3, outline=ICON_COLOUR, width=line)
    dot(15, 4.5, 2.6, fill=ICON_COLOUR)
    dot(15, 15.5, 2.6, fill=ICON_COLOUR)
    return big.resize((size, size), Image.Resampling.LANCZOS)


def write_icon(path: Path) -> None:
    """Save the mark as a multi-size .ico, for the Windows shortcut."""
    path.parent.mkdir(parents=True, exist_ok=True)
    sizes = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (256, 256)]
    # BMP entries, not Pillow's default PNG ones: Explorer can show a blank
    # shortcut icon for small PNG-compressed sizes.
    draw_icon(256).save(path, format="ICO", sizes=sizes, bitmap_format="bmp")


def alert(message: str) -> None:
    """Tell the user something went wrong, even without a console."""
    if sys.platform == "win32":
        import ctypes

        MB_ICONERROR = 0x10
        ctypes.windll.user32.MessageBoxW(None, message, APP_NAME, MB_ICONERROR)
    else:
        print(f"{APP_NAME}: {message}", file=sys.stderr)


def redirect_output(log_dir: Path) -> TextIO | None:
    """Send output to a log file when there is no console (``pythonw``).

    Without a console ``sys.stderr`` is None, and uvicorn's log handlers
    would have nowhere to write.
    """
    if sys.stderr is not None:
        return None
    log_dir.mkdir(parents=True, exist_ok=True)
    log = open(log_dir / LOG_FILE_NAME, "a", encoding="utf-8", buffering=1)  # noqa: SIM115
    sys.stdout = sys.stderr = log
    return log


def run_tray(settings: Settings) -> int:
    import pystray

    url = dashboard_url(settings.host, settings.port)
    if already_running(url):
        webbrowser.open(url)
        return 0

    server = ServerThread(settings, log_level="warning")
    try:
        server.start()
    except RuntimeError as exc:
        alert(f"Could not start on {settings.host}:{settings.port}: {exc}")
        return 1

    webbrowser.open(url)  # launching it means you want to look at it
    open_once = Debounce(OPEN_DEBOUNCE_SECONDS)

    def open_dashboard(icon: "pystray.Icon", item: "pystray.MenuItem") -> None:
        # A click runs this; a double-click arrives as two clicks.
        if open_once():
            webbrowser.open(url)

    def quit_app(icon: "pystray.Icon", item: "pystray.MenuItem") -> None:
        server.stop()
        icon.stop()

    menu = pystray.Menu(
        pystray.MenuItem("Open dashboard", open_dashboard, default=True),
        pystray.MenuItem(f"Running on {settings.host}:{settings.port}", None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit", quit_app),
    )
    icon = pystray.Icon(APP_NAME, draw_icon(), f"{APP_NAME} · {url}", menu)
    icon.run()  # blocks until quit_app calls icon.stop()
    if server.running:  # the icon went away some other way
        server.stop()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agent-monitor-tray", description=__doc__.splitlines()[0])
    parser.add_argument(
        "--write-icon", type=Path, metavar="PATH", help="save the icon as a .ico file and exit"
    )
    args = parser.parse_args(argv)
    if args.write_icon:
        write_icon(args.write_icon)
        return 0

    settings = Settings()
    redirect_output(settings.db_path.parent)
    return run_tray(settings)


if __name__ == "__main__":
    sys.exit(main())
