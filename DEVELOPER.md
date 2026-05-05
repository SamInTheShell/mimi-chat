# Developer notes

This is a working notebook of "where things live" and "how to poke at them" for
mimi-chat. None of it is formal yet — read it as the kind of context another
human dev would want before touching anything.

## Repo layout

```
src/
  main.py        — app entrypoint, Qt/webview wiring, JsApi bridge exposed to the frontend
  tools.py       — sandboxed filesystem tools (preview + apply pairs, dispatch)
  storage.py     — JSON persistence under ~/.mimi-chat/, defaults for config/tools/prompts
  ignore.py      — gitignore-style filter used by the tools sandbox
  prompts/       — built-in system prompts (one .md per persona), seeded into storage
  logo.py        — splash / about text
  frontend/
    index.html   — the entire UI (HTML + CSS + JS in one file). State, chat loop,
                   tool dispatch, settings panes — all in here.
    theme.css, markdown.css, favicon.svg
    assets/      — vendored fonts, marked, dompurify, highlight.js, etc.
pyproject.toml   — uv/hatchling build, declares the `mimi-chat` script
uv.lock          — pinned deps (pywebview, PySide6, qtpy, pathspec)
```

State on disk: `~/.mimi-chat/{config,prompts,sampling,tools,ignore}.json`. If
something looks wrong with persistence, read those JSON files directly.

## Running

```
uv run mimi-chat            # normal launch
uv run mimi-chat --debug    # opens Chromium DevTools attached to the window
uv run mimi-chat --verbose  # backend discovery / load messages on stderr
```

In-app: `Ctrl/Cmd+Shift+I` opens DevTools any time. Settings header has a
"DevTools" button too.

## Debugging Python

The backend is plain Python with no test suite yet. Two patterns cover most
work:

**Quick smoke test of a module via `uv run python -c`.** This is how the tool
helpers were validated when added — drop into a temp dir, call the public
entrypoints, print results. No fixtures, no framework, just check the shapes:

```bash
uv run python -c "
from src import tools
import tempfile, pathlib
with tempfile.TemporaryDirectory() as d:
    root = pathlib.Path(d)
    (root/'a.txt').write_text('hi')
    (root/'sub').mkdir()
    print('preview:', tools.preview_call('move', {'from':'a.txt','to':'sub/b.txt'}, str(root)))
    print('apply:  ', tools.call('move',         {'from':'a.txt','to':'sub/b.txt'}, str(root)))
    print('refuse: ', tools.preview_call('move', {'from':'sub','to':'sub/inner'}, str(root)))
"
```

`tools.preview_call` / `tools.call` already wrap exceptions into
`{ok, error, error_kind}`, so you see the same shape the frontend will see.
For things that mutate `~/.mimi-chat/`, use `CONFIG_DIR` overrides or a temp
HOME — don't pollute your real config.

**Live backend inspection via DevTools.** Once the app is running, anything
exposed on `JsApi` (see `main.py`) is callable from the JS console as
`window.pywebview.api.<method>(...)`. That includes `tool_preview`, `tool_call`,
`load_all`, `validate_dir`, etc. So you can drive the Python side from the
DevTools console without restarting the app.

## Debugging JavaScript

All the frontend code lives in `src/frontend/index.html`. There is no build
step — what you see is what runs.

**The trick: globals are intentional.** State (`st`), the tool catalog
(`builtinTools`), the API bridge (`mimiApi`), and most helpers
(`buildApiMessages`, `_renderToolCardBody`, `buildToolsPayload`, ...) are
plain top-level globals. Open DevTools (`Ctrl/Cmd+Shift+I` or launch with
`--debug`) and call them from the console:

```js
// Inspect / mutate live state
st.projectDir
st.conversationMessages
st.autoAcceptEdits = true

// Exercise tools end-to-end through the same path the chat loop uses
await mimiApi.toolPreview('move', { from: 'a.txt', to: 'b.txt' }, st.projectDir)
await mimiApi.toolCall   ('move', { from: 'a.txt', to: 'b.txt' }, st.projectDir)

// Render a card directly without going through the chat loop
_renderToolCardBody({ kind: 'move', from: 'a', to: 'b', kind_of_source: 'file' })
```

**Pure-UI work without pywebview.** You can also open `src/frontend/index.html`
directly in a regular Chromium/Firefox to iterate on layout, CSS, and pure-JS
code paths. `window.pywebview` is undefined in that mode — `mimiApi.has()`
returns false and any `mimiApi.*` call short-circuits to a synthetic error —
which is fine for tweaking styles or rendering helpers but useless for
anything that touches the filesystem. The DevTools that ship with the desktop
build are the same Chromium ones, so console + sources + network all work as
expected.

**Reload after edits.** pywebview doesn't hot-reload. After editing
`index.html`, restart the app (`Ctrl+C` then `uv run mimi-chat --debug`).
DevTools' "Empty Cache and Hard Reload" works for assets but the page itself
needs the restart because pywebview loads it as a `file://` URL once.

## Validating a new tool end-to-end

A tool touches four places. If any of them is missing the symptom is silent
(LLM never sees it / UI shows raw JSON / permission prompt looks empty), so
walk this checklist when adding one:

1. **Backend impl + dispatch — `src/tools.py`**
   - Add a `*_preview` (or `*_compute`) and `*_apply` pair. Preview should be
     cheap and side-effect-free; apply should produce the real result.
   - Register the tool name in both `preview()` and `execute()` dispatchers.
   - Use `_safe_join`, `_refuse_if_ignored`, and the `_arg_*` coercion helpers
     — don't trust LLM-supplied args.

2. **Default permission — `src/storage.py`**
   - Add the tool name to `DEFAULT_TOOLS` with `"allow"` (read-only) or
     `"ask"` (anything that mutates).

3. **Frontend registration — `src/frontend/index.html`**
   - `DEFAULT_TOOL_PERMS` (mirror of `storage.DEFAULT_TOOLS`).
   - `builtinTools` array — `id`, `name`, `perm`, and a `desc` the LLM will
     read. Keep the description action-oriented and document the args inline.
   - `TOOL_PARAM_SCHEMAS` — the JSON Schema that ships to the model in the
     `tools` payload.
   - `_renderToolCardBody` — add a `kind === '<your_tool>'` branch so the
     preview/result card renders something readable instead of a JSON dump.

4. **Smoke test the four layers**

   a. **Python contract** — call `tools.preview_call` / `tools.call` from
      `uv run python -c` against a temp dir (see the snippet above). Cover
      the happy path, ignore-list refusal, sandbox-escape refusal, and
      the main "won't do that" branches you wrote.

   b. **Bridge** — start the app with `--debug`, open DevTools, run:
      ```js
      await mimiApi.toolPreview('<your_tool>', { /* args */ }, st.projectDir)
      await mimiApi.toolCall   ('<your_tool>', { /* args */ }, st.projectDir)
      ```
      Confirm the shape matches what `_renderToolCardBody` expects.

   c. **UI** — in DevTools console, render a fake card to eyeball the
      layout without involving the model:
      ```js
      _renderToolCardBody({ kind: '<your_tool>', /* fields */ })
      ```
      Or trigger it from the model and watch the permission modal +
      history card.

   d. **End-to-end with the model** — point the chat at a small local
      model, ask it to use the new tool, and verify: tool appears in the
      `tools` payload (Network tab → request body), permission modal shows
      a sensible preview, the apply result lands in
      `st.conversationMessages` as a `tool` message, and the next turn
      proceeds.

## Things that bite

- **Path persistence is tilde-form on disk** (`~/foo`). Always round-trip
  through `storage.expand_user_path` / `collapse_to_tilde` at the boundary.
  Filesystem ops use absolute paths; storage and the UI use the tilde form.
- **The ignore filter is loaded per call** in `tools.py` (`_load_filter`).
  Editing `~/.mimi-chat/ignore.json` while the app is running takes effect
  on the next tool call — no restart needed.
- **DevTools opens in a sibling Qt window**, not via the upstream pywebview
  `debug=True` redirect to `chrome-devtools-frontend.appspot.com`. See
  `_open_local_devtools` in `main.py` if it ever stops working.
- **`--disable-web-security` is set** for the embedded Chromium so JS can
  call local LM Studio / Ollama servers that don't send CORS headers. This
  is only safe because the page is `file://` and not user-navigable. Don't
  load remote URLs into the main webview.
