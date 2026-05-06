import argparse
import os
import sys
from pathlib import Path

from . import storage, tools


class JsApi:
    """Bridge exposed to the webview JS as ``window.pywebview.api.*``.

    Methods accept/return JSON-serializable data; paths are tilde-form on the
    wire and on disk (see ``storage.collapse_to_tilde``). Filesystem checks
    expand ``~/`` internally before touching the disk.
    """

    def __init__(self) -> None:
        self._window = None

    def set_window(self, window) -> None:
        self._window = window

    # ── storage ──────────────────────────────
    def load_all(self):
        return storage.load_all()

    def save_config(self, partial):
        return storage.save_config(partial or {})

    def save_prompts(self, payload):
        return storage.save_prompts(payload or {})

    def save_sampling(self, payload):
        return storage.save_sampling(payload or {})

    def save_tools(self, payload):
        return storage.save_tools(payload or {})

    def save_ignore(self, payload):
        return storage.save_ignore(payload or {})

    def add_recent_dir(self, path):
        return storage.add_recent_dir(path or "")

    def remove_recent_dir(self, path):
        return storage.remove_recent_dir(path or "")

    # ── path helpers ─────────────────────────
    def collapse_to_tilde(self, path):
        return storage.collapse_to_tilde(path or "")

    def expand_user_path(self, path):
        return storage.expand_user_path(path or "")

    def validate_dir(self, path):
        s = (path or "").strip()
        if not s:
            return {"ok": False, "reason": "Project directory is required.", "absolute": "", "display": ""}
        absolute = storage.expand_user_path(s)
        p = Path(absolute)
        if not p.exists():
            return {"ok": False, "reason": "Path does not exist.", "absolute": absolute, "display": storage.collapse_to_tilde(absolute)}
        if not p.is_dir():
            return {"ok": False, "reason": "Path is not a directory.", "absolute": absolute, "display": storage.collapse_to_tilde(absolute)}
        return {"ok": True, "reason": "", "absolute": absolute, "display": storage.collapse_to_tilde(absolute)}

    # ── tools ────────────────────────────────
    def tool_preview(self, name, args, project_dir):
        return tools.preview_call(name or "", args or {}, project_dir or "")

    def tool_call(self, name, args, project_dir):
        return tools.call(name or "", args or {}, project_dir or "")

    def pick_folder(self, initial=None):
        """Open the native folder picker. Returns tilde-collapsed path or ``""``."""
        if self._window is None:
            return ""
        import webview

        start = storage.expand_user_path(initial) if initial else str(Path.home())
        try:
            sel = self._window.create_file_dialog(webview.FOLDER_DIALOG, directory=start)
        except Exception:
            return ""
        if not sel:
            return ""
        first = sel[0] if isinstance(sel, (list, tuple)) else sel
        return storage.collapse_to_tilde(str(first))

    def save_text_file(self, default_filename, content, initial_dir=None, file_types=None):
        """Open the native save dialog and write ``content`` to the chosen path.

        ``file_types`` is an optional list/tuple of filter strings like
        ``"Markdown (*.md)"``; when omitted we offer Markdown + All files.
        Returns ``{ok, path, cancelled, error}``: ``cancelled`` is true when the
        user dismissed the dialog (no error). Path comes back tilde-collapsed.
        """
        if self._window is None:
            return {"ok": False, "path": "", "cancelled": False, "error": "No window available."}
        import webview

        start = storage.expand_user_path(initial_dir) if initial_dir else str(Path.home())
        fname = (default_filename or "conversation.md").strip() or "conversation.md"
        if isinstance(file_types, (list, tuple)) and file_types:
            ftypes = tuple(str(x) for x in file_types if x)
        else:
            ftypes = ("Markdown (*.md)", "All files (*.*)")
        try:
            sel = self._window.create_file_dialog(
                webview.SAVE_DIALOG,
                directory=start,
                save_filename=fname,
                file_types=ftypes,
            )
        except Exception as e:
            return {"ok": False, "path": "", "cancelled": False, "error": f"Save dialog failed: {e}"}
        if not sel:
            return {"ok": False, "path": "", "cancelled": True, "error": ""}
        first = sel[0] if isinstance(sel, (list, tuple)) else sel
        target = Path(str(first))
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(str(content or ""), encoding="utf-8")
        except Exception as e:
            return {"ok": False, "path": str(target), "cancelled": False, "error": f"Write failed: {e}"}
        return {"ok": True, "path": storage.collapse_to_tilde(str(target)), "cancelled": False, "error": ""}

    # ── clipboard ────────────────────────────
    def copy_to_clipboard(self, text):
        """Copy text to the system clipboard (works from file:// origins)."""
        import subprocess, sys
        try:
            if sys.platform == "darwin":
                subprocess.run(["pbcopy"], input=(text or "").encode(), check=True)
            elif sys.platform == "win32":
                subprocess.run(["clip"], input=(text or "").encode("utf-16-le"), check=True)
            else:
                subprocess.run(["xclip", "-selection", "clipboard"], input=(text or "").encode(), check=True)
            return True
        except Exception:
            return False

    # ── devtools ─────────────────────────────
    def open_devtools(self):
        """Open (or re-show) the local DevTools window. Same panel ``--debug`` opens at startup."""
        if self._window is None:
            return False
        try:
            _open_local_devtools(self._window)
            return True
        except Exception:
            return False


def _resolve_window_geometry(stored):
    """Pick (width, height, x, y) for window creation from stored config.

    Drops x/y to ``None`` (OS-centered) when the saved position would land the
    window off every connected screen — happens after a monitor disconnect or a
    DE that previously parked the window at a sentinel like (-32000, -32000).
    Creates a ``QApplication`` if none exists yet; pywebview's qt backend will
    reuse the same instance when ``webview.start`` runs.
    """
    DEFAULT_W, DEFAULT_H = 800, 750
    MIN_OVERLAP = 100
    geom = stored if isinstance(stored, dict) else {}

    def _i(v):
        try:
            return int(v)
        except (TypeError, ValueError):
            return None

    width = _i(geom.get("width")) or DEFAULT_W
    height = _i(geom.get("height")) or DEFAULT_H
    x = _i(geom.get("x"))
    y = _i(geom.get("y"))

    if x is None or y is None:
        return width, height, None, None

    try:
        from qtpy.QtGui import QGuiApplication
        from qtpy.QtWidgets import QApplication

        if QApplication.instance() is None:
            QApplication(sys.argv)
        screens = QGuiApplication.screens()
        if not screens:
            return width, height, None, None
        for s in screens:
            r = s.availableGeometry()
            ox = max(x, r.x())
            oy = max(y, r.y())
            ex = min(x + width, r.x() + r.width())
            ey = min(y + height, r.y() + r.height())
            if ex - ox >= MIN_OVERLAP and ey - oy >= MIN_OVERLAP:
                return width, height, x, y
    except Exception:
        return width, height, x, y
    return width, height, None, None


def _configure_qt_app_identity() -> None:
    """Make Linux Alt-Tab / taskbar show "Mimi" instead of "python3".

    Qt picks up the app name from sys.argv[0] when QApplication is constructed,
    which is the interpreter path under uv. X11 WMs read it as WM_CLASS and
    Wayland compositors as the app_id, so the task switcher labels the window
    after the binary unless we override these explicitly. Set on QCoreApplication
    so the values stick before pywebview's qt backend instantiates QApplication.
    """
    from qtpy.QtCore import QCoreApplication
    from qtpy.QtGui import QGuiApplication

    QCoreApplication.setApplicationName("Mimi Chat")
    QCoreApplication.setOrganizationName("Mimi Chat")
    QGuiApplication.setApplicationDisplayName("Mimi Chat")
    QGuiApplication.setDesktopFileName("Mimi Chat")


def _configure_qt_webengine_for_local_llm_apis() -> None:
    """Let in-webview JS call local OpenAI-compatible servers (LM Studio, Ollama, etc.).

    Those servers often omit ``Access-Control-Allow-Origin``. Qt WebEngine is Chromium;
    this relaxes the same-origin policy for the desktop app only—not for remote pages the
    user might load. All HTTP to LLM APIs stays in JavaScript (``fetch``), as intended.
    """
    prev = os.environ.get("QTWEBENGINE_CHROMIUM_FLAGS", "").strip()
    extra = "--disable-web-security"
    os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = f"{prev} {extra}".strip() if prev else extra


HOTKEYS_HELP = """\
hotkeys:
  Global
    Ctrl/Cmd+,             Open Settings
    Ctrl/Cmd+Shift+I       Open Chromium DevTools

  Chat screen
    Ctrl/Cmd+I             Focus the message composer
    Enter                  Send message
    Shift+Enter            Insert a line break
    Ctrl/Cmd+T             Toggle extended thinking
    Shift+Tab              Toggle Accept-Edits (auto-approve tool calls)
    Escape                 Stop the in-flight reply
    /                      Open the slash-command menu

  Slash-command menu
    Up / Down              Move highlight
    Enter / Tab            Run the highlighted command
    Escape                 Close the menu
    /clear                 Clear conversation context
    /think                 Toggle extended thinking
    /autoedit              Toggle Accept-Edits
    /model                 Open the model picker
    /system                Open the system-prompt picker
    /save-md               Export the conversation as Markdown
    /save-json             Export the raw conversation + tool payload as JSON (debug)

  Model / system-prompt pickers
    Up / Down              Move highlight
    Enter                  Select the highlighted item
    Escape                 Close the picker

  Tool-permission modal
    Ctrl/Cmd+Enter         Allow the call
    Escape                 Deny the call
    Alt+Escape             Focus the deny-reason field

  Setup screen
    Ctrl/Cmd+Enter         Start the session

  Settings screen
    Ctrl+PageUp / PageDown Cycle tabs (Linux/Windows)
    Cmd+Shift+[ / ]        Cycle tabs (macOS)
"""


def main():
    """Initialize and start the webview application."""
    parser = argparse.ArgumentParser(
        prog="mimi-chat",
        description="Mimi - a local-first chat UI for OpenAI-compatible LLM servers (LM Studio, Ollama, llama.cpp, etc.).",
        epilog=HOTKEYS_HELP,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Open the Chromium DevTools panel attached to the main window at startup. (You can also open it any time from the Settings header or with Ctrl/Cmd+Shift+I.)",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print backend discovery and frontend-load messages to stderr.",
    )
    args = parser.parse_args()

    stderr = None
    devnull = None
    if not args.verbose:
        stderr = sys.stderr
        devnull = open(os.devnull, "w")
        sys.stderr = devnull

    try:
        _configure_qt_webengine_for_local_llm_apis()
        _configure_qt_app_identity()
        import webview

        html_file = Path(__file__).parent / "frontend" / "index.html"
        # as_uri() — not str() — so pywebview's is_local_url() check sees a
        # file:// scheme and skips spinning up its bundled bottle HTTP server.
        url = html_file.as_uri()
        if args.verbose:
            print(f"Loading frontend from {html_file}")
            if not html_file.exists():
                print(f"WARNING: Frontend not found at {html_file}")

        cfg = storage.load_config()
        win_w, win_h, win_x, win_y = _resolve_window_geometry(cfg.get("windowGeometry"))

        api = JsApi()
        window = webview.create_window(
            title="Mimi Chat",
            url=url,
            width=win_w,
            height=win_h,
            x=win_x,
            y=win_y,
            min_size=(950, 400),
            resizable=True,
            js_api=api,
        )
        api.set_window(window)

        def _persist_window_geometry():
            try:
                storage.save_config({
                    "windowGeometry": {
                        "x": int(window.x) if window.x is not None else None,
                        "y": int(window.y) if window.y is not None else None,
                        "width": int(window.width) if window.width is not None else win_w,
                        "height": int(window.height) if window.height is not None else win_h,
                    }
                })
            except Exception:
                pass

        window.events.closing += _persist_window_geometry

        _install_devtools_shortcut(window)

        if args.debug:
            window.events.loaded += lambda: _open_local_devtools(window)

        webview.start(gui="qt")

    finally:
        if devnull:
            sys.stderr = stderr
            devnull.close()


def _install_devtools_shortcut(window):
    """Bind Ctrl+Shift+I to open DevTools, on the Qt side rather than in JS.

    Why Qt-side: a JS-bound hotkey breaks the moment the page errors out — i.e.
    exactly when DevTools is needed most. Qt.ApplicationShortcut also makes the
    binding fire while focus is inside the embedded Chromium view, which a plain
    QShortcut wouldn't.

    QShortcut must be constructed on the Qt main thread, but window.events.loaded
    fires on a pywebview worker thread. The qt backend's BrowserView exposes
    create_window_trigger as the official way to bounce a callable onto the GUI
    thread.
    """
    holder = {"shortcut": None}

    def install_shortcut():
        if holder["shortcut"] is not None:
            return
        from qtpy.QtCore import Qt
        from qtpy.QtGui import QKeySequence
        from qtpy.QtWidgets import QShortcut
        from webview.platforms.qt import BrowserView

        bv = BrowserView.instances.get(window.uid)
        if bv is None:
            return
        shortcut = QShortcut(QKeySequence("Ctrl+Shift+I"), bv)
        shortcut.setContext(Qt.ApplicationShortcut)
        shortcut.activated.connect(lambda: _open_local_devtools(window))
        holder["shortcut"] = shortcut

    def schedule_install():
        from webview.platforms.qt import BrowserView

        bv = BrowserView.instances.get(window.uid)
        if bv is None:
            return
        bv.create_window_trigger.emit(install_shortcut)

    window.events.loaded += schedule_install


def _open_local_devtools(window):
    """Attach Chromium's bundled DevTools to the main page in a sibling Qt window.

    Why: pywebview's debug=True exposes QTWEBENGINE_REMOTE_DEBUGGING, whose
    inspector page redirects to chrome-devtools-frontend.appspot.com. Using
    QWebEnginePage.setDevToolsPage() instead keeps the entire DevTools UI local.
    """
    from qtpy.QtCore import QTimer
    from qtpy.QtWidgets import QApplication
    from qtpy.QtWebEngineWidgets import QWebEngineView
    from webview.platforms.qt import BrowserView

    def attach():
        bv = BrowserView.instances.get(window.uid)
        if bv is None:
            return
        existing = getattr(bv, "_devtools_view", None)
        if existing is not None:
            # Re-show if the user closed the DevTools window via its X button.
            existing.show()
            existing.raise_()
            existing.activateWindow()
            return
        devtools_view = QWebEngineView()
        devtools_view.setWindowTitle(f"DevTools - {window.title}")
        devtools_view.resize(1000, 700)
        bv.webview.page().setDevToolsPage(devtools_view.page())
        devtools_view.show()
        bv._devtools_view = devtools_view

    QTimer.singleShot(0, QApplication.instance(), attach)


if __name__ == "__main__":
    main()
