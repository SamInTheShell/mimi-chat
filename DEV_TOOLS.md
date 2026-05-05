# Local Chromium DevTools in pywebview (the Qt backend)

How to wire up classic, local-only Chromium DevTools — the same panel you get
from `Ctrl+Shift+I` in a normal browser — for a pywebview app, and how to
trigger it from inside the page with a hotkey.

This is **not** what `webview.start(gui='qt', debug=True)` does. That switches
on `QTWEBENGINE_REMOTE_DEBUGGING`, which exposes a remote-debugging endpoint
whose inspector frontend redirects to `chrome-devtools-frontend.appspot.com`.
The instant your machine is offline, or your security model says no third
parties, that's useless. What we want is the bundled DevTools that ships
inside QtWebEngine's Chromium, attached as a sibling Qt window. No HTTP, no
appspot, no redirect.

## How it actually works

QtWebEngine exposes `QWebEnginePage.setDevToolsPage(otherPage)`. When you
hand it a second page, Chromium loads the local `devtools://` inspector UI
into that page and wires it to the inspected page over an internal channel.
You render the second page in a normal `QWebEngineView`, and that view *is*
the DevTools window. No remote debugging port involved.

Pywebview's qt backend keeps its `BrowserView` wrappers in
`webview.platforms.qt.BrowserView.instances`, keyed by `window.uid`. From
that wrapper, `bv.webview.page()` returns the underlying `QWebEnginePage`.
That's the page you want to inspect.

The only annoying bit is timing: `webview.start()` is blocking, the
`BrowserView` doesn't exist until the window has been created on the Qt
event loop, and the page isn't fully ready until `loaded` fires. Defer the
attach with `QTimer.singleShot(0, ...)` or hang it off `window.events.loaded`.

## Minimal hello-world

```python
# hello.py — local DevTools, no appspot redirect, Ctrl+Shift+I from inside the page.
import webview
from qtpy.QtCore import QTimer
from qtpy.QtWidgets import QApplication
from qtpy.QtWebEngineWidgets import QWebEngineView

HTML = """
<!doctype html>
<meta charset="utf-8">
<title>Hello</title>
<h1>Hello, DevTools</h1>
<p>Press <kbd>Ctrl/Cmd+Shift+I</kbd> to open local DevTools.</p>
<script>
  // Forward the hotkey to Python. The webview itself doesn't bind F12 / Ctrl+Shift+I
  // unless you set debug=True (which we don't, on purpose).
  document.addEventListener('keydown', (e) => {
    if ((e.key === 'i' || e.key === 'I') && (e.ctrlKey || e.metaKey) && e.shiftKey && !e.altKey) {
      e.preventDefault();
      window.pywebview.api.open_devtools();
    }
  }, true);
</script>
"""


def open_local_devtools(window):
    """Attach Chromium's bundled DevTools to `window` in a sibling Qt window."""
    from webview.platforms.qt import BrowserView

    def attach():
        bv = BrowserView.instances.get(window.uid)
        if bv is None:
            return  # window not built yet; the QTimer call below handles the retry
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
        view.show()
        bv._devtools_view = view  # keep a ref so it isn't GC'd

    # Defer onto the Qt event loop so BrowserView.instances is populated.
    QTimer.singleShot(0, QApplication.instance(), attach)


class Api:
    def __init__(self):
        self._window = None

    def bind(self, window):
        self._window = window

    def open_devtools(self):
        if self._window is None:
            return False
        open_local_devtools(self._window)
        return True


def main():
    api = Api()
    window = webview.create_window("Hello", html=HTML, js_api=api)
    api.bind(window)
    # NOTE: no debug=True — we don't want the remote-debugging / appspot redirect.
    webview.start(gui="qt")


if __name__ == "__main__":
    main()
```

Run with `uv run python hello.py` (or `python hello.py` in any env that has
`pywebview`, `PySide6`, and `qtpy` installed). Press Ctrl+Shift+I — a second
window opens with classic, fully-local Chromium DevTools.

### Why a JS hotkey instead of a Python `QShortcut`

You could install a `QShortcut` on the `QWebEngineView`, but the inner page
swallows most key events before Qt's shortcut machinery sees them. Catching
the keystroke in JS with capture-phase `addEventListener('keydown', …, true)`
and bouncing it through the `pywebview` API is the path of least resistance,
and it's what mimi-chat's main app does.

## Auto-opening on startup (optional)

Hang the attach off `loaded`:

```python
window = webview.create_window("Hello", html=HTML, js_api=api)
api.bind(window)
window.events.loaded += lambda: open_local_devtools(window)
webview.start(gui="qt")
```

## Opening straight to the Console panel

Chromium DevTools persists "the last panel I had open" in its own
`localStorage`, under the key `panel-selectedTab`. There's no public Qt API
to pick a panel, but you can preset that key on the DevTools page itself
right after it loads. Once it's set, *every* subsequent open lands on
Console — first open will still show whatever DevTools defaults to (usually
Elements) and silently flip to Console on its first reopen, unless you also
force a reload.

Add a `loadFinished` hook to the DevTools page:

```python
def open_local_devtools(window, default_panel="console"):
    from webview.platforms.qt import BrowserView

    def attach():
        bv = BrowserView.instances.get(window.uid)
        if bv is None:
            return
        existing = getattr(bv, "_devtools_view", None)
        if existing is not None:
            existing.show(); existing.raise_(); existing.activateWindow()
            return
        view = QWebEngineView()
        view.setWindowTitle(f"DevTools - {window.title}")
        view.resize(1000, 700)
        bv.webview.page().setDevToolsPage(view.page())

        def pin_panel(ok):
            if not ok:
                return
            # Persist the panel choice in DevTools' own localStorage,
            # then force-reload so it takes effect on this session too.
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
            # Only do this on the *first* loadFinished after attach.
            try:
                view.page().loadFinished.disconnect(pin_panel)
            except Exception:
                pass

        view.page().loadFinished.connect(pin_panel)
        view.show()
        bv._devtools_view = view

    QTimer.singleShot(0, QApplication.instance(), attach)
```

Notes on this approach:

- The first time a fresh user profile opens DevTools, you'll briefly see
  Elements before the reload swaps in Console. After that, the preference
  sticks and there's no flicker.
- `panel-selectedTab` is an internal DevTools key, not a Qt API. It's been
  stable across recent Chromium versions but is not contractually
  guaranteed. If a future QtWebEngine bumps to a Chromium that renames it,
  this stops working silently — DevTools just opens to its own default.
- Other valid values include `'elements'`, `'sources'`, `'network'`,
  `'application'`, `'performance'`. Match the panel id from DevTools' own
  tab buttons.
- If you'd rather avoid the reload, you can drop the `location.reload()`
  and accept that *only* the second-and-later opens land on Console.

If you want a no-internal-keys alternative: after the inspect window is
focused, send Esc once to open the bottom drawer (which is the Console)
underneath whatever panel is currently showing. That gives you a console
prompt without poking at DevTools internals, at the cost of not actually
making Console the primary panel.

## Common things that go wrong

- **`BrowserView.instances` is empty / `KeyError`.** You called the attach
  before pywebview built the Qt window. Wrap the body in
  `QTimer.singleShot(0, QApplication.instance(), attach)` (the QTimer must
  be parented to the Qt app, not to a not-yet-existing widget) or hang
  the call off `window.events.loaded`.
- **DevTools window vanishes immediately.** You didn't keep a reference to
  the `QWebEngineView`. Stash it on the `BrowserView` (or anywhere
  long-lived) — Qt will garbage-collect it as soon as the local goes out
  of scope.
- **Closing the DevTools X-button kills it permanently.** `setDevToolsPage`
  doesn't recreate the view. The example above handles this by re-`show()`ing
  the existing view if `bv._devtools_view` is already set, instead of
  re-attaching.
- **`Ctrl+Shift+I` doesn't fire.** A focused element with its own keydown
  handler called `stopPropagation()`. Register the listener with the
  capture-phase third arg (`addEventListener('keydown', fn, true)`).
- **You see a page that says "Inspectable WebContents" with a link to
  `chrome-devtools-frontend.appspot.com`.** You're using
  `webview.start(..., debug=True)`. Remove the flag — that's the path you
  *don't* want. Your `setDevToolsPage` route doesn't need it.
- **Imports of `webview.platforms.qt` fail.** That module is only loaded
  when the qt backend has been selected. Either pin
  `webview.start(gui="qt")` first, or import lazily inside the attach
  function (the example does this).
