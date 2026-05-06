# pywebview frontend kickstarter

The opinionated baseline for a new pywebview app: load the frontend straight
off the filesystem (no helper HTTP server) and bind `Ctrl+Shift+I` to a local
Chromium DevTools window. See `DEV_TOOLS.md` for the full DevTools rationale —
this doc is the short, copy-pasteable starting point.

## What this gives you

- A pywebview window that loads `frontend/index.html` directly via `file://` —
  **no loopback HTTP listener**.
- `Ctrl+Shift+I` opens classic, fully-local Chromium DevTools in a sibling Qt
  window. No `chrome-devtools-frontend.appspot.com` redirect.
- A `--debug` flag that auto-opens DevTools on load.

## Project layout

```
your_app/
├── pyproject.toml
└── src/
    ├── frontend/
    │   └── index.html
    └── your_app/
        ├── __init__.py
        └── app.py
```

`pyproject.toml` dependencies:

```toml
dependencies = [
    "pyside6>=6.11.0",
    "pywebview>=6.2.1",
    "qtpy>=2.4.3",
]
```

## `app.py` — the whole skeleton

```python
import sys
from pathlib import Path

import webview
from qtpy.QtCore import Qt, QTimer
from qtpy.QtGui import QKeySequence
from qtpy.QtWidgets import QApplication, QShortcut
from qtpy.QtWebEngineWidgets import QWebEngineView


PACKAGE_DIR = Path(__file__).resolve().parent
FRONTEND_INDEX = PACKAGE_DIR.parent / "frontend" / "index.html"
DEFAULT_DEVTOOLS_PANEL = "console"


def open_local_devtools(window, default_panel=DEFAULT_DEVTOOLS_PANEL):
    from webview.platforms.qt import BrowserView

    def attach():
        bv = BrowserView.instances.get(window.uid)
        if bv is None:
            return
        existing = getattr(bv, "_devtools_view", None)
        if existing is not None:
            existing.show()
            existing.raise_()
            existing.activateWindow()
            return
        view = QWebEngineView()
        view.setWindowTitle(f"DevTools - {window.title}")
        view.resize(1000, 700)
        bv.webview.page().setDevToolsPage(view.page())

        def pin_panel(ok):
            if not ok:
                return
            view.page().runJavaScript(
                f"""
                (function() {{
                    try {{
                        if (localStorage.getItem('panel-selectedTab') !== '{default_panel}') {{
                            localStorage.setItem('panel-selectedTab', '{default_panel}');
                            location.reload();
                        }}
                    }} catch (e) {{}}
                }})();
                """
            )
            try:
                view.page().loadFinished.disconnect(pin_panel)
            except Exception:
                pass

        view.page().loadFinished.connect(pin_panel)
        view.show()
        bv._devtools_view = view

    QTimer.singleShot(0, QApplication.instance(), attach)


def main():
    window = webview.create_window("Frontend", url=FRONTEND_INDEX.as_uri())

    holder = {"shortcut": None}

    def install_shortcut():
        if holder["shortcut"] is not None:
            return
        from webview.platforms.qt import BrowserView
        bv = BrowserView.instances.get(window.uid)
        if bv is None:
            return
        shortcut = QShortcut(QKeySequence("Ctrl+Shift+I"), bv)
        shortcut.setContext(Qt.ApplicationShortcut)
        shortcut.activated.connect(lambda: open_local_devtools(window))
        holder["shortcut"] = shortcut

    def schedule_install():
        from webview.platforms.qt import BrowserView
        bv = BrowserView.instances.get(window.uid)
        if bv is None:
            return
        bv.create_window_trigger.emit(install_shortcut)

    window.events.loaded += schedule_install

    if "--debug" in sys.argv:
        window.events.loaded += lambda: open_local_devtools(window)

    webview.start(gui="qt")


if __name__ == "__main__":
    main()
```

## Why `FRONTEND_INDEX.as_uri()` matters

pywebview's `create_window(url=...)` runs the value through
`is_local_url()` (`webview/util.py`). Anything that doesn't start with
`http://`, `https://`, or `file://` is classified as "local" and triggers
pywebview's bundled `bottle` HTTP server, which binds to `127.0.0.1` on a
random port. Pass a bare path string (`str(path)`) and you get that listener.
Pass a real `file://` URI (`path.as_uri()`) and pywebview skips the server
entirely — the page loads straight off disk.

Verify with `netstat -plnt | grep python`. After this change, nothing should
be listening until you press `Ctrl+Shift+I` (which opens its own Chromium
DevTools loopback channel — that one is unavoidable while DevTools is
attached, but it disappears again when DevTools is closed).

### `file://` tradeoffs

Chromium treats every `file://` document as a unique opaque origin and
applies stricter rules than over HTTP:

- `fetch()` / `XMLHttpRequest` to other local files is blocked.
- ES module imports (`<script type="module">`) won't load sibling files.
- `localStorage` / `IndexedDB` may behave differently per-file.

If you outgrow these limits, switch back to letting pywebview serve the
folder (drop `as_uri()` and pass `str(FRONTEND_INDEX)`), or wire up your own
WSGI/ASGI app via `url=app`. For a pure-static UI that just talks to
Python through `js_api`, `file://` is fine and keeps the process boundary
clean.

## Why the shortcut is installed via `create_window_trigger`

`window.events.loaded` fires on a pywebview worker thread. `QShortcut` must
be constructed on the Qt main thread. The qt backend's `BrowserView` exposes
a `create_window_trigger` signal that pywebview itself uses to bounce work
onto the GUI thread — emit it with a callable and Qt will run that callable
on the main thread. That's `schedule_install` → `install_shortcut`.

`shortcut.setContext(Qt.ApplicationShortcut)` makes the binding fire even
when focus is inside the embedded web view. Without it, key events get
swallowed by Chromium before the QShortcut machinery sees them. (`DEV_TOOLS.md`
discusses an alternative: catching the keystroke in JS and bouncing it
through `js_api` — that approach is also fine, particularly if you'd rather
not depend on `Qt.ApplicationShortcut` semantics.)

## The `--debug` flag

`--debug` only controls whether DevTools auto-opens on first load. The
`Ctrl+Shift+I` shortcut is always installed, so opening DevTools on demand
works in any run. We deliberately do **not** pass `debug=True` to
`webview.start()` — that flag enables `QTWEBENGINE_REMOTE_DEBUGGING`, which
exposes a remote-debugging endpoint whose inspector frontend redirects to
`chrome-devtools-frontend.appspot.com`. We want the bundled local DevTools
instead, which is exactly what `setDevToolsPage()` gives us.

## Quick sanity checklist for a new project

1. `dependencies` includes `pywebview`, `pyside6`, `qtpy`.
2. `create_window(url=...)` receives a `file://` URI (use `Path.as_uri()`).
3. `webview.start(gui="qt")` — qt backend, no `debug=True`.
4. `Ctrl+Shift+I` installed via `create_window_trigger.emit(install_shortcut)`.
5. `netstat -plnt | grep python` is empty before DevTools is opened.
