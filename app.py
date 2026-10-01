"""Daily Digest: your mail and your news in one window.

Usage:
    pythonw app.py      # open the app (no console window)
    python app.py       # same, but with a console showing progress

It runs a small server on this computer only (127.0.0.1) and opens it in an
Edge app window. The mail and news code live in mail_digest.py and
news_digest.py next to this file, and both scripts still work on their own.
The server stops by itself a few minutes after you close the window.
"""

import datetime as dt
import html
import importlib.util
import json
import os
import socket
import subprocess
import sys
import threading
import time
import traceback
import urllib.request
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# Without this, one stuck mail or news connection hangs its tab on "Refreshing…" for good.
socket.setdefaulttimeout(60)

if sys.stdout is None:  # pythonw has no console; keep print() from crashing
    sys.stdout = sys.stderr = open(os.devnull, "w")

HERE = Path(__file__).resolve().parent
MAIL_SCRIPT = HERE / "mail_digest.py"
NEWS_SCRIPT = HERE / "news_digest.py"
# %APPDATA% on Windows; elsewhere a hidden folder, so it can't land inside a clone at ~/daily-digest.
STATE_DIR = Path(os.environ["APPDATA"]) / "daily-digest" if "APPDATA" in os.environ else Path.home() / ".daily-digest"
PORT_FILE = STATE_DIR / "port"
# The mail page lists your mail, so mail_digest.py keeps it next to its settings (its OUTPUT_FILE).
MAIL_PAGE = Path(os.environ.get("APPDATA", Path.home())) / "mail-digest" / "digest.html"
PING_REPLY = f"daily-digest {HERE}"  # names this copy's folder, so a copy elsewhere isn't mistaken for it

MAIL_DAYS = 14
NEWS_HOURS = 48
AUTO_REFRESH_MIN = 30          # rebuild both tabs this often while the window is open
IDLE_SHUTDOWN_SEC = 10 * 60    # stop the server this long after the window goes away

EDGE_PATHS = [
    Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Microsoft/Edge/Application/msedge.exe",
    Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Microsoft/Edge/Application/msedge.exe",
    Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Google/Chrome/Application/chrome.exe",
]

# Same palette as the news page, so both tabs look alike.
DARK_MAIL = """<style>:root { color-scheme:dark; --bg:#0e0e12; --card:#17171d; --text:#ececf1;
  --muted:#9696a3; --line:#26262f; --accent:#9d85ff; --chip:#24242c; --err:#3a1f1c; }</style>"""
EMBED_NEWS = "<style>.bar h1, .stamp { display:none; }</style>"  # the app's own top bar covers these


def load(name, path):
    if not path.exists():
        if ".zip" in str(HERE).lower():  # Explorer runs a file from inside a ZIP by copying just that file to Temp
            raise FileNotFoundError("app.py was opened from inside the ZIP, so the files next to it aren't there. "
                                    "Right-click the ZIP, choose Extract All, then run app.py from the extracted folder.")
        raise FileNotFoundError(f"{path} is missing. Download the whole repository, not just app.py.")
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except ModuleNotFoundError as e:
        raise ModuleNotFoundError(f"{e}. In a terminal in {HERE}, run: pip install -r requirements.txt") from e
    return mod


def message_page(title, body):
    return f"""<!doctype html><html><head><meta charset="utf-8">
<style>body {{ margin:0; background:#0e0e12; color:#ececf1; font:15px/1.5 "Segoe UI", system-ui, sans-serif; }}
main {{ max-width:560px; margin:80px auto; padding:0 16px; }} h1 {{ font-size:20px; }}
p {{ color:#9696a3; }} code {{ background:#17171d; padding:2px 6px; border-radius:6px; color:#ececf1; }}</style>
</head><body><main><h1>{title}</h1>{body}</main></body></html>"""


# ---------------------------------------------------------------- builders

def build_mail():
    mail = load("mail_digest", MAIL_SCRIPT)
    cfg = mail.load_config()
    if not cfg["accounts"]:
        return message_page("No mail accounts yet",
                            f"<p>Add one from a terminal in <code>{HERE}</code>:</p>"
                            "<p><code>python mail_digest.py add</code></p><p>Then press Refresh.</p>")

    def one(acct):
        try:
            return mail.fetch_account(acct, cfg, MAIL_DAYS, False), None
        except Exception as e:  # noqa: BLE001
            return [], (acct["email"], str(e))

    with ThreadPoolExecutor(max_workers=len(cfg["accounts"])) as pool:
        results = list(pool.map(one, cfg["accounts"]))
    items = mail.dedupe([i for got, _ in results for i in got])
    errors = [err for _, err in results if err]
    if len(errors) == len(cfg["accounts"]):  # nothing worked (offline?): keep the last good page
        raise RuntimeError("couldn't check any account. " + "; ".join(f"{a}: {m}" for a, m in errors))
    page = mail.render(items, errors, cfg["accounts"], MAIL_DAYS)
    mail.save_digest(page)
    return page


def build_news():
    news = load("news_digest", NEWS_SCRIPT)
    with ThreadPoolExecutor(max_workers=3) as pool:
        a = pool.submit(news.fetch_news, NEWS_HOURS)
        b = pool.submit(news.fetch_stocks)
        c = pool.submit(news.fetch_crypto)
        (stories, e1), (stocks, e2), (crypto, e3) = a.result(), b.result(), c.result()
    errors = e1 + e2 + e3
    if errors and not (stories or stocks or crypto):  # nothing worked (offline?): keep the last good page
        raise RuntimeError(f"couldn't reach any news or price site. {errors[0][0]}: {errors[0][1]}")
    page = news.render(stories, stocks, crypto, errors, NEWS_HOURS)
    news.OUTPUT_FILE.write_text(page, encoding="utf-8")
    return page


class Tab:
    def __init__(self, name, builder, cache_file, extra_css):
        self.name, self.builder, self.extra_css = name, builder, extra_css
        self.lock = threading.Lock()
        self.html, self.updated, self.error = None, None, None
        self.building, self.version = False, 0
        self.last_try = None  # when the last build finished, whether it worked or not
        if cache_file.exists():  # show the last result straight away while refreshing
            self.html = cache_file.read_text(encoding="utf-8")
            self.updated = dt.datetime.fromtimestamp(cache_file.stat().st_mtime).astimezone()

    def refresh(self):
        with self.lock:
            if self.building:
                return
            self.building = True
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        print(f"Building {self.name}...")
        try:
            self.html = self.builder()
            self.updated = dt.datetime.now().astimezone()
            self.error = None
            self.version += 1  # only a new page counts, so a failed refresh doesn't show "New updates"
            print(f"  {self.name} done")
        except Exception as e:  # noqa: BLE001
            self.error = str(e)
            traceback.print_exc()
        finally:
            self.last_try = time.monotonic()
            self.building = False

    def page(self):
        if self.html is None:
            return message_page(f"Couldn't load {self.name}", f"<p>{html.escape(self.error)}</p>") if self.error else ""
        return self.html.replace("</head>", self.extra_css + "</head>", 1)

    def status(self):
        return {"building": self.building, "version": self.version, "error": self.error,
                "ready": self.html is not None,
                "updated": self.updated.isoformat() if self.updated else None}


TABS = {
    "mail": Tab("mail", build_mail, MAIL_PAGE, DARK_MAIL),
    "news": Tab("news", build_news, NEWS_SCRIPT.parent / "news.html", EMBED_NEWS),
}
last_seen = time.monotonic()


# ---------------------------------------------------------------- server

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send(self, body, ctype="text/html; charset=utf-8", status=200):
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def allowed(self):
        # Only answer requests addressed to this server, so other websites can't reach it.
        port = self.server.server_address[1]
        return self.headers.get("Host") in (f"127.0.0.1:{port}", f"localhost:{port}")

    def do_GET(self):
        global last_seen
        if not self.allowed():
            return self.send("Forbidden", "text/plain", 403)
        path = self.path.split("?", 1)[0]
        if path == "/":
            self.send(SHELL)
        elif path == "/ping":
            self.send(PING_REPLY, "text/plain")
        elif path == "/status":
            last_seen = time.monotonic()
            self.send(json.dumps({k: t.status() for k, t in TABS.items()}), "application/json")
        elif path.strip("/") in TABS:
            self.send(TABS[path.strip("/")].page())
        elif path == "/icon.svg":
            self.send(ICON_SVG, "image/svg+xml")
        else:
            self.send("Not found", "text/plain", 404)

    def do_POST(self):
        if not self.allowed():
            return self.send("Forbidden", "text/plain", 403)
        name = self.path.split("?", 1)[0].removeprefix("/refresh/")
        if self.path.startswith("/refresh/") and name in TABS:
            TABS[name].refresh()
            self.send("ok", "text/plain")
        else:
            self.send("Not found", "text/plain", 404)


def running_port():
    """Port of an already-running copy of the app from this same folder, if there is one."""
    try:
        port = int(PORT_FILE.read_text())
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/ping", timeout=2) as r:
            if r.read().decode("utf-8") == PING_REPLY:
                return port
    except Exception:  # noqa: BLE001
        pass
    return None


def open_window(port):
    url = f"http://127.0.0.1:{port}/"
    for exe in EDGE_PATHS:
        if exe.exists():
            subprocess.Popen([str(exe), f"--app={url}", "--window-size=1280,900"])
            return
    webbrowser.open(url)


def background_loop(server):
    while True:
        time.sleep(30)
        idle = time.monotonic() - last_seen
        if idle > IDLE_SHUTDOWN_SEC:
            print("Window closed; stopping.")
            server.shutdown()
            return
        for tab in TABS.values():
            # Count from the last attempt, not the last success, so a failing tab
            # waits the full interval before trying again.
            if tab.last_try is not None and time.monotonic() - tab.last_try > AUTO_REFRESH_MIN * 60:
                tab.refresh()


def main():
    port = running_port()
    if port:  # already open somewhere: just show another window
        print(f"Already running at http://127.0.0.1:{port}/ - opening another window.")
        open_window(port)
        return

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    PORT_FILE.write_text(str(port))

    for tab in TABS.values():
        tab.refresh()
    threading.Thread(target=background_loop, args=(server,), daemon=True).start()
    open_window(port)
    print(f"Daily Digest running at http://127.0.0.1:{port}/")
    try:
        server.serve_forever()
    finally:
        try:  # leave it alone if a copy from another folder has taken over since
            if PORT_FILE.read_text() == str(port):
                PORT_FILE.unlink()
        except OSError:
            pass


# ---------------------------------------------------------------- shell page

ICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">
<rect width="64" height="64" rx="14" fill="#9d85ff"/>
<rect x="14" y="17" width="36" height="6" rx="3" fill="#0e0e12"/>
<rect x="14" y="29" width="36" height="6" rx="3" fill="#0e0e12" opacity=".7"/>
<rect x="14" y="41" width="22" height="6" rx="3" fill="#0e0e12" opacity=".45"/></svg>"""

SHELL = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Daily Digest</title>
<link rel="icon" href="/icon.svg">
<style>
:root { color-scheme:dark; --bg:#0e0e12; --card:#17171d; --text:#ececf1; --muted:#9696a3;
        --line:#26262f; --accent:#9d85ff; --accent-soft:#241f3d; --err:#ff6b6b; }
* { box-sizing:border-box; }
html, body { height:100%; margin:0; }
body { background:var(--bg); color:var(--text); font:15px/1.45 "Segoe UI", system-ui, sans-serif;
       display:flex; flex-direction:column; }
.top { display:flex; align-items:center; gap:18px; padding:10px 16px; border-bottom:1px solid var(--line); }
.brand { display:flex; align-items:center; gap:9px; font-weight:700; font-size:16px; }
.brand img { width:22px; height:22px; }
.tabs { display:flex; gap:4px; background:var(--card); padding:4px; border-radius:11px; }
.tabs button { font:inherit; font-weight:600; font-size:14px; color:var(--muted); background:none; border:0;
               padding:6px 16px; border-radius:8px; cursor:pointer; display:flex; align-items:center; gap:7px; }
.tabs button:hover { color:var(--text); }
.tabs button.on { background:var(--accent); color:var(--bg); }
.dot { width:7px; height:7px; border-radius:50%; background:currentColor; opacity:0; }
.tabs button.busy .dot { opacity:1; animation:pulse 1s infinite alternate; }
@keyframes pulse { to { opacity:.25; } }
.right { margin-left:auto; display:flex; align-items:center; gap:12px; }
#stamp { color:var(--muted); font-size:13px; }
#stamp.err { color:var(--err); }
#refresh { font:inherit; font-size:13px; font-weight:600; color:var(--text); background:var(--card);
           border:1px solid var(--line); border-radius:9px; padding:6px 12px; cursor:pointer;
           display:flex; align-items:center; gap:6px; }
#refresh:hover { border-color:var(--accent); }
#refresh:disabled { opacity:.5; cursor:default; }
#refresh.spin svg { animation:spin 1s linear infinite; }
@keyframes spin { to { transform:rotate(360deg); } }
#new { display:none; font:inherit; font-size:13px; font-weight:600; background:var(--accent-soft); color:var(--accent);
       border:0; border-radius:9px; padding:6px 12px; cursor:pointer; }
.panes { flex:1; position:relative; }
iframe { position:absolute; inset:0; width:100%; height:100%; border:0; background:var(--bg); }
.loading { position:absolute; inset:0; display:flex; flex-direction:column; align-items:center; justify-content:center;
           gap:14px; color:var(--muted); }
.spinner { width:30px; height:30px; border:3px solid var(--line); border-top-color:var(--accent); border-radius:50%;
           animation:spin .8s linear infinite; }
[hidden] { display:none !important; }
@media (max-width:640px) { .brand span, #stamp { display:none; } }
</style></head>
<body>
<div class="top">
  <div class="brand"><img src="/icon.svg" alt=""><span>Daily Digest</span></div>
  <div class="tabs">
    <button data-tab="mail"><span class="dot"></span>Mail</button>
    <button data-tab="news"><span class="dot"></span>News</button>
  </div>
  <div class="right">
    <button id="new">New updates · show</button>
    <span id="stamp"></span>
    <button id="refresh" title="Refresh this tab">
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"
           stroke-linecap="round"><path d="M21 12a9 9 0 1 1-2.64-6.36"/><path d="M21 3v6h-6"/></svg>Refresh</button>
  </div>
</div>
<div class="panes">
  <iframe id="f-mail" hidden title="Mail"></iframe>
  <iframe id="f-news" hidden title="News"></iframe>
  <div class="loading" id="loading" hidden><div class="spinner"></div><div id="loading-text"></div></div>
</div>
<script>
const TABS = ["mail", "news"];
const WAIT = { mail: "Checking your inboxes…", news: "Fetching the latest news…" };
// Opening the app counts as asking for fresh results, so they replace the cached page when ready.
let active = "mail", status = {}, loaded = {}, pending = {}, userAsked = { mail: true, news: true };
try { active = localStorage.getItem("tab") || active; } catch (e) {}
if (!TABS.includes(active)) active = "mail";

function ago(iso) {
  const m = Math.round((Date.now() - new Date(iso)) / 60000);
  if (m < 1) return "just now";
  if (m < 60) return m + "m ago";
  const h = Math.round(m / 60);
  return h < 24 ? h + "h ago" : Math.round(h / 24) + "d ago";
}
function load(tab) {
  document.getElementById("f-" + tab).src = "/" + tab + "?v=" + status[tab].version;
  loaded[tab] = status[tab].version;
  pending[tab] = false;
}
function draw() {
  const s = status[active] || {};
  document.querySelectorAll(".tabs button").forEach(b => {
    const st = status[b.dataset.tab] || {};
    b.classList.toggle("on", b.dataset.tab === active);
    b.classList.toggle("busy", !!st.building);
  });
  TABS.forEach(t => document.getElementById("f-" + t).hidden = t !== active || !(status[t] || {}).ready);
  document.getElementById("loading").hidden = !!s.ready;
  document.getElementById("loading-text").textContent = s.error ? "Couldn't load: " + s.error : WAIT[active];
  const stamp = document.getElementById("stamp");
  stamp.classList.toggle("err", !!s.error);
  stamp.title = s.error || "";  // the last good page stays up, so hovering is where you see why
  stamp.textContent = s.building ? "Refreshing…" : s.error ? "Last refresh failed"
                    : s.updated ? "Updated " + ago(s.updated) : "";
  const r = document.getElementById("refresh");
  r.disabled = !!s.building;
  r.classList.toggle("spin", !!s.building);
  document.getElementById("new").style.display = pending[active] ? "block" : "none";
}
async function poll() {
  try {
    status = await (await fetch("/status")).json();
  } catch (e) {
    document.getElementById("stamp").textContent = "App stopped — reopen it from the shortcut";
    return;
  }
  TABS.forEach(t => {
    const st = status[t];
    if (!st.ready || st.version === loaded[t]) return;
    // First load, or a refresh you asked for: show it now. Auto-refreshes wait for a click
    // so the page doesn't jump while you're reading.
    if (loaded[t] === undefined || userAsked[t] || t !== active) {
      load(t);
      if (!st.building) userAsked[t] = false;  // cached page shown while building: keep waiting for the fresh one
    }
    else pending[t] = true;
  });
  draw();
}
document.querySelectorAll(".tabs button").forEach(b => b.onclick = () => {
  active = b.dataset.tab;
  try { localStorage.setItem("tab", active); } catch (e) {}
  if (pending[active]) load(active);
  draw();
});
document.getElementById("refresh").onclick = async () => {
  userAsked[active] = true;
  await fetch("/refresh/" + active, { method: "POST" });
  poll();
};
document.getElementById("new").onclick = () => { load(active); draw(); };
function onKey(e) {
  if (e.key === "F5" || (e.ctrlKey && e.key === "r")) { e.preventDefault(); document.getElementById("refresh").click(); }
}
document.addEventListener("keydown", onKey);
// After a click inside a tab, keys go to its frame instead of this page, so listen there too.
TABS.forEach(t => document.getElementById("f-" + t).addEventListener("load", e => {
  try { e.target.contentDocument.addEventListener("keydown", onKey); } catch (err) {}
}));
poll();
setInterval(poll, 3000);
</script>
</body></html>"""


if __name__ == "__main__":
    main()
