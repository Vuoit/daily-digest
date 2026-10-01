"""Mail digest: check all your inboxes and condense unread mail into one page.

Usage:
    python mail_digest.py add            # add an account (you'll be prompted)
    python mail_digest.py list           # show configured accounts
    python mail_digest.py remove EMAIL   # remove an account and its saved sign-in
    python mail_digest.py                # build the digest and open it
    python mail_digest.py --days 30      # look further back
    python mail_digest.py --all          # include Gmail promotions/social tabs

Inboxes are opened read-only, so nothing gets marked as read.
Passwords are kept in your system's password store (Windows Credential Manager,
macOS Keychain, ...) via `keyring`, never in files.
"""

import argparse
import datetime as dt
import email
import html
import imaplib
import json
import os
import re
import ssl
import sys
import webbrowser
from email import policy
from email.utils import parseaddr, parsedate_to_datetime
from getpass import getpass
from html.parser import HTMLParser
from pathlib import Path

import keyring

APP = "mail-digest"
CONFIG_DIR = Path(os.environ.get("APPDATA", Path.home())) / APP
ACCOUNTS_FILE = CONFIG_DIR / "accounts.json"
MS_CACHE_FILE = CONFIG_DIR / "ms_token_cache.json"
OUTPUT_FILE = Path(__file__).resolve().parent / "digest.html"

MAX_PER_ACCOUNT = 300          # newest N unread messages per account
MAX_BODY_BYTES = 1_500_000     # skip downloading bigger messages (attachments)
MS_SCOPES = ["https://outlook.office.com/IMAP.AccessAsUser.All"]

PROVIDERS = {
    "gmail":   {"host": "imap.gmail.com",       "auth": "password"},
    "yahoo":   {"host": "imap.mail.yahoo.com",  "auth": "password"},
    "icloud":  {"host": "imap.mail.me.com",     "auth": "password"},
    "outlook": {"host": "outlook.office365.com", "auth": "oauth"},
}
DOMAIN_PROVIDER = {
    "gmail.com": "gmail", "googlemail.com": "gmail",
    "outlook.com": "outlook", "hotmail.com": "outlook", "live.com": "outlook",
    "msn.com": "outlook", "hotmail.co.uk": "outlook", "outlook.co.uk": "outlook",
    "yahoo.com": "yahoo", "ymail.com": "yahoo", "rocketmail.com": "yahoo",
    "yahoo.co.uk": "yahoo", "aol.com": "yahoo",
    "icloud.com": "icloud", "me.com": "icloud", "mac.com": "icloud",
}
APP_PASSWORD_HELP = {
    "gmail": "Google Account > Security > 2-Step Verification > App passwords "
             "(https://myaccount.google.com/apppasswords)",
    "yahoo": "Yahoo Account Info > Account security > Generate app password",
    "icloud": "https://account.apple.com > Sign-In and Security > App-Specific Passwords",
}

IMPORTANT = re.compile(
    r"\b(invoice|payment|past due|overdue|due (date|soon|today|tomorrow)|bill|"
    r"statement|refund|tax|bank|verify|verification|security alert|sign[- ]in|"
    r"password|suspicious|locked|action required|urgent|deadline|expir\w*|"
    r"appointment|interview|offer|contract|renewal|cancel\w*|failed|declined|"
    r"reminder|delivery|shipped|court|jury|dmv|insurance|claim)\b",
    re.I,
)
AUTOMATED_SENDER = re.compile(
    r"(no-?reply|do-?not-?reply|notifications?|newsletter|marketing|mailer|"
    r"bounce|updates?|news|info|hello|team)@",
    re.I,
)
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


# ---------------------------------------------------------------- config

def load_config():
    if ACCOUNTS_FILE.exists():
        return json.loads(ACCOUNTS_FILE.read_text(encoding="utf-8"))
    return {"accounts": [], "outlook_client_id": ""}


def save_config(cfg):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    ACCOUNTS_FILE.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


def cmd_add(cfg):
    addr = input("Email address: ").strip().lower()
    if "@" not in addr:
        sys.exit("That doesn't look like an email address.")
    if any(a["email"] == addr for a in cfg["accounts"]):
        sys.exit(f"{addr} is already set up. Remove it first to redo it.")

    provider = DOMAIN_PROVIDER.get(addr.split("@", 1)[1])
    if not provider:
        choice = input("Provider? [gmail/outlook/yahoo/icloud/other]: ").strip().lower()
        provider = choice if choice in PROVIDERS else "other"

    if provider == "other":
        host = input("IMAP server (e.g. imap.example.com): ").strip()
        acct = {"email": addr, "provider": "other", "host": host, "auth": "password"}
    else:
        acct = {"email": addr, "provider": provider, **PROVIDERS[provider]}

    if acct["auth"] == "password":
        if provider in APP_PASSWORD_HELP:
            print(f"\n{provider.title()} needs an app password, not your normal one.")
            print(f"Create one here: {APP_PASSWORD_HELP[provider]}\n")
        secret = getpass("App password (hidden as you type): ").replace(" ", "")
        keyring.set_password(APP, addr, secret)
    else:
        if not cfg.get("outlook_client_id"):
            print("\nOutlook needs a one-time app registration (see README.md, "
                  "'Outlook setup').")
            cfg["outlook_client_id"] = input("Application (client) ID: ").strip()
        outlook_token(cfg["outlook_client_id"], addr, interactive=True)

    print("Testing connection...")
    try:
        imap = connect(acct, cfg)
        imap.logout()
    except Exception as e:  # noqa: BLE001
        if acct["auth"] == "password":
            keyring.delete_password(APP, addr)
        sys.exit(f"Couldn't sign in: {e}")

    cfg["accounts"].append(acct)
    save_config(cfg)
    print(f"Added {addr}.")


def cmd_remove(cfg, addr):
    addr = addr.lower()
    before = len(cfg["accounts"])
    cfg["accounts"] = [a for a in cfg["accounts"] if a["email"] != addr]
    if len(cfg["accounts"]) == before:
        sys.exit(f"{addr} isn't set up.")
    try:
        keyring.delete_password(APP, addr)
    except keyring.errors.PasswordDeleteError:
        pass
    save_config(cfg)
    print(f"Removed {addr}.")


# ---------------------------------------------------------------- sign-in

def outlook_token(client_id, addr, interactive=False):
    import msal

    cache = msal.SerializableTokenCache()
    if MS_CACHE_FILE.exists():
        cache.deserialize(MS_CACHE_FILE.read_text(encoding="utf-8"))
    app = msal.PublicClientApplication(
        client_id,
        authority="https://login.microsoftonline.com/consumers",
        token_cache=cache,
    )
    result = None
    for account in app.get_accounts(username=addr):
        result = app.acquire_token_silent(MS_SCOPES, account=account)
        if result:
            break
    if not result:
        if not interactive:
            raise RuntimeError("Outlook sign-in expired; run `add` again for this account")
        flow = app.initiate_device_flow(scopes=MS_SCOPES)
        if "user_code" not in flow:
            raise RuntimeError(flow.get("error_description", "couldn't start sign-in"))
        print("\n" + flow["message"] + "\n")
        result = app.acquire_token_by_device_flow(flow)
    if "access_token" not in result:
        raise RuntimeError(result.get("error_description", "Outlook sign-in failed"))
    if cache.has_state_changed:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        MS_CACHE_FILE.write_text(cache.serialize(), encoding="utf-8")
    return result["access_token"]


def connect(acct, cfg):
    # imaplib skips the certificate check unless it's given a context. Without one, anyone
    # on the same Wi-Fi could pose as the mail server and receive your password.
    imap = imaplib.IMAP4_SSL(acct["host"], timeout=60, ssl_context=ssl.create_default_context())
    if acct["auth"] == "oauth":
        token = outlook_token(cfg["outlook_client_id"], acct["email"])
        auth = f"user={acct['email']}\x01auth=Bearer {token}\x01\x01".encode()
        imap.authenticate("XOAUTH2", lambda _: auth)
    else:
        secret = keyring.get_password(APP, acct["email"])
        if not secret:
            raise RuntimeError("no saved password; run `add` again for this account")
        imap.login(acct["email"], secret)
    return imap


# ---------------------------------------------------------------- fetching

def parse_fetch(data):
    """Turn an imaplib FETCH response into [(meta_bytes, payload_bytes)]."""
    records = []
    for item in data:
        if isinstance(item, tuple):
            records.append([item[0], item[1]])
        elif isinstance(item, bytes) and records:
            records[-1][0] += b" " + item  # trailing items such as ' UID 5)'
    return records


def chunks(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def search_unread(imap, acct, days, include_all):
    if acct["provider"] == "gmail":
        query = f"is:unread newer_than:{days}d"
        if not include_all:
            query += " -category:promotions -category:social -category:forums"
        typ, data = imap.uid("SEARCH", "X-GM-RAW", f'"{query}"')
    else:
        since = dt.date.today() - dt.timedelta(days=days)
        since_str = f"{since.day:02d}-{MONTHS[since.month - 1]}-{since.year}"
        typ, data = imap.uid("SEARCH", "UNSEEN", "SINCE", since_str)
    if typ != "OK":
        raise RuntimeError(f"search failed: {data}")
    uids = sorted((data[0] or b"").split(), key=int)
    return uids[-MAX_PER_ACCOUNT:]


def fetch_account(acct, cfg, days, include_all):
    imap = connect(acct, cfg)
    try:
        typ, _ = imap.select("INBOX", readonly=True)
        if typ != "OK":
            raise RuntimeError("couldn't open inbox")
        uids = search_unread(imap, acct, days, include_all)

        sizes, headers = {}, {}
        for chunk in chunks(uids, 50):
            _, data = imap.uid("FETCH", b",".join(chunk).decode(),
                               "(UID RFC822.SIZE BODY.PEEK[HEADER])")
            for meta, payload in parse_fetch(data):
                uid = re.search(rb"UID (\d+)", meta)
                size = re.search(rb"RFC822\.SIZE (\d+)", meta)
                if uid:
                    headers[uid.group(1)] = payload
                    sizes[uid.group(1)] = int(size.group(1)) if size else 0

        bodies = {}
        small = [u for u in headers if sizes[u] <= MAX_BODY_BYTES]
        for chunk in chunks(small, 20):
            _, data = imap.uid("FETCH", b",".join(chunk).decode(), "(UID BODY.PEEK[])")
            for meta, payload in parse_fetch(data):
                uid = re.search(rb"UID (\d+)", meta)
                if uid:
                    bodies[uid.group(1)] = payload
    finally:
        try:
            imap.logout()
        except Exception:  # noqa: BLE001
            pass

    return [to_item(acct["email"], bodies.get(u) or headers[u], u in bodies)
            for u in headers]


# ---------------------------------------------------------------- condensing

class _TextOnly(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts, self._skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "head"):
            self._skip += 1

    def handle_endtag(self, tag):
        if tag in ("script", "style", "head") and self._skip:
            self._skip -= 1

    def handle_data(self, d):
        if not self._skip:
            self.parts.append(d)


def snippet_of(msg):
    try:
        part = msg.get_body(preferencelist=("plain", "html"))
        if part is None:
            return ""
        text = part.get_content()
        if part.get_content_type() == "text/html":
            p = _TextOnly()
            p.feed(text)
            text = " ".join(p.parts)
    except Exception:  # noqa: BLE001
        return ""
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:280] + ("…" if len(text) > 280 else "")


def to_item(account, raw, has_body):
    msg = email.message_from_bytes(raw, policy=policy.default)
    name, addr = parseaddr(str(msg.get("From", "")))
    subject = str(msg.get("Subject", "")).strip() or "(no subject)"
    try:
        date = parsedate_to_datetime(str(msg.get("Date")))
        if date.tzinfo is None:
            date = date.replace(tzinfo=dt.timezone.utc)
        # Convert to local time here, so a date Windows can't handle (before 1970, or far in
        # the future, as some spam has) counts as no date instead of breaking the whole page.
        date = date.astimezone()
    except Exception:  # noqa: BLE001
        date = None

    precedence = str(msg.get("Precedence", "")).lower()
    auto = str(msg.get("Auto-Submitted", "no")).lower()
    automated = bool(
        msg.get("List-Unsubscribe") or msg.get("List-Id")
        or precedence in ("bulk", "list", "junk")
        or auto not in ("", "no")
        or AUTOMATED_SENDER.search(addr or "")
    )
    return {
        "account": account,
        "from_name": name or addr,
        "from_addr": addr.lower(),
        "subject": subject,
        "date": date,
        "snippet": snippet_of(msg) if has_body else "(large message — probably has attachments)",
        "automated": automated,
        "important": bool(IMPORTANT.search(subject)),
        "message_id": str(msg.get("Message-ID", "")).strip(),
    }


def dedupe(items):
    seen, out = set(), []
    for it in items:
        key = it["message_id"] or (it["from_addr"], it["subject"], it["date"])
        if key in seen:
            continue
        seen.add(key)
        out.append(it)
    return out


# ---------------------------------------------------------------- output

def fmt_date(d):
    if not d:
        return ""
    return d.strftime("%a %b ") + str(d.day) + d.strftime(", %I:%M %p").replace(" 0", " ")


def render_item(it):
    e = html.escape
    return f"""
      <li class="item">
        <div class="row">
          <span class="from" title="{e(it['from_addr'])}">{e(it['from_name'])}</span>
          <span class="date">{e(fmt_date(it['date']))}</span>
        </div>
        <div class="subject">{e(it['subject'])}</div>
        <div class="snippet">{e(it['snippet'])}</div>
        <div class="acct">to {e(it['account'])}</div>
      </li>"""


def render(items, errors, accounts, days):
    sort_key = lambda i: i["date"] or dt.datetime.min.replace(tzinfo=dt.timezone.utc)  # noqa: E731
    items.sort(key=sort_key, reverse=True)
    attention = [i for i in items if i["important"]]
    people = [i for i in items if not i["important"] and not i["automated"]]
    automated = [i for i in items if not i["important"] and i["automated"]]

    groups = {}
    for it in automated:
        groups.setdefault(it["from_addr"], []).append(it)
    group_html = "".join(
        f"""<details class="group"><summary><span class="from">{html.escape(g[0]['from_name'])}</span>
        <span class="count">{len(g)}</span></summary><ul>{''.join(render_item(i) for i in g)}</ul></details>"""
        for g in sorted(groups.values(), key=len, reverse=True)
    )

    per_account = {a["email"]: 0 for a in accounts}
    for it in items:
        per_account[it["account"]] = per_account.get(it["account"], 0) + 1
    chips = "".join(
        f'<span class="chip">{html.escape(a)} <b>{n}</b></span>' for a, n in per_account.items()
    )
    err_html = "".join(
        f'<div class="error">Couldn\'t check <b>{html.escape(a)}</b>: {html.escape(m)}</div>'
        for a, m in errors
    )

    def section(title, lst, empty):
        body = f"<ul>{''.join(render_item(i) for i in lst)}</ul>" if lst else f'<p class="empty">{empty}</p>'
        return f'<section><h2>{title} <span class="count">{len(lst)}</span></h2>{body}</section>'

    now = dt.datetime.now()
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Mail Digest</title>
<style>
:root {{ --bg:#f7f7f5; --card:#fff; --text:#1d1d1f; --muted:#6b6b70; --line:#e4e4e0;
        --accent:#b4462b; --chip:#ecebe6; --err:#fdecea; }}
@media (prefers-color-scheme: dark) {{
  :root {{ --bg:#161617; --card:#1f1f21; --text:#ececec; --muted:#9a9aa0; --line:#2e2e31;
          --accent:#f08a6c; --chip:#2a2a2d; --err:#3a1f1c; }} }}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--bg); color:var(--text);
       font:15px/1.45 "Segoe UI", system-ui, sans-serif; }}
main {{ max-width:760px; margin:0 auto; padding:32px 16px 64px; }}
h1 {{ font-size:26px; margin:0 0 4px; }}
.sub {{ color:var(--muted); margin:0 0 16px; }}
.chips {{ display:flex; flex-wrap:wrap; gap:6px; margin-bottom:24px; }}
.chip {{ background:var(--chip); border-radius:999px; padding:3px 10px; font-size:13px; }}
h2 {{ font-size:17px; margin:28px 0 10px; }}
.count {{ color:var(--muted); font-weight:400; font-size:14px; }}
ul {{ list-style:none; margin:0; padding:0; }}
.item {{ background:var(--card); border:1px solid var(--line); border-radius:10px;
        padding:12px 14px; margin-bottom:8px; }}
section:first-of-type .item {{ border-left:3px solid var(--accent); }}
.row {{ display:flex; justify-content:space-between; gap:12px; }}
.from {{ font-weight:600; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }}
.date, .acct {{ color:var(--muted); font-size:13px; white-space:nowrap; }}
.subject {{ margin-top:2px; }}
.snippet {{ color:var(--muted); font-size:14px; margin-top:4px; overflow-wrap:anywhere; }}
.group {{ background:var(--card); border:1px solid var(--line); border-radius:10px;
         margin-bottom:8px; }}
.group summary {{ padding:10px 14px; cursor:pointer; display:flex; gap:8px; }}
.group ul {{ padding:0 8px 4px; }}
.group .item {{ border:none; border-top:1px solid var(--line); border-radius:0; }}
.empty {{ color:var(--muted); }}
.error {{ background:var(--err); border-radius:8px; padding:10px 14px; margin-bottom:8px; }}
</style></head>
<body><main>
<h1>Mail Digest</h1>
<p class="sub">{len(items)} unread from the last {days} days · built {now:%b} {now.day}, {now:%I:%M %p}</p>
<div class="chips">{chips}</div>
{err_html}
{section("Needs attention", attention, "Nothing flagged — no bills, security alerts or deadlines.")}
{section("From people", people, "No unread mail from real people.")}
<section><h2>Automated &amp; newsletters <span class="count">{len(automated)}</span></h2>
{group_html or '<p class="empty">None.</p>'}</section>
</main></body></html>"""


# ---------------------------------------------------------------- main

def cmd_run(cfg, days, include_all, open_browser):
    if not cfg["accounts"]:
        sys.exit("No accounts yet. Run:  python mail_digest.py add")
    items, errors = [], []
    for acct in cfg["accounts"]:
        print(f"Checking {acct['email']}...", end=" ", flush=True)
        try:
            got = fetch_account(acct, cfg, days, include_all)
            items.extend(got)
            print(f"{len(got)} unread")
        except Exception as e:  # noqa: BLE001
            errors.append((acct["email"], str(e)))
            print(f"failed ({e})")
    items = dedupe(items)
    OUTPUT_FILE.write_text(render(items, errors, cfg["accounts"], days), encoding="utf-8")
    print(f"\nDigest written to {OUTPUT_FILE}")
    if open_browser:
        webbrowser.open(OUTPUT_FILE.as_uri())


def main():
    ap = argparse.ArgumentParser(description="Condense unread mail from all your accounts.")
    ap.add_argument("command", nargs="?", default="run", choices=["run", "add", "list", "remove"])
    ap.add_argument("email", nargs="?")
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--all", action="store_true", help="include Gmail promotions/social/forums")
    ap.add_argument("--no-open", action="store_true", help="don't open the digest in a browser")
    args = ap.parse_args()
    cfg = load_config()

    if args.command == "add":
        cmd_add(cfg)
    elif args.command == "list":
        for a in cfg["accounts"]:
            print(f"{a['email']:40} {a['provider']}")
        if not cfg["accounts"]:
            print("No accounts yet.")
    elif args.command == "remove":
        if not args.email:
            sys.exit("Usage: python mail_digest.py remove EMAIL")
        cmd_remove(cfg, args.email)
    else:
        cmd_run(cfg, args.days, args.all, not args.no_open)


if __name__ == "__main__":
    main()
