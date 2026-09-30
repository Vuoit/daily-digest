# Daily Digest

Your mail and your news in one window. Open it from the **Daily Digest** shortcut on your desktop.

- **Mail** tab: the [Mail Digest](../Mail%20Digest) page (unread mail from the last 14 days).
- **News** tab: the [News Digest](../News%20Digest) page (gaming news, then stocks and crypto).

When the app opens it shows your last results straight away, then fetches fresh ones in the
background. **Refresh** (or F5) rebuilds the tab you're on. While the window is open, both tabs
refresh themselves every 30 minutes. When that happens, a "New updates" button appears instead
of the page jumping while you read.

The app loads the code from the `Mail Digest` and `News Digest` folders next to this one,
so keep the three folders side by side. Both scripts still work on their own. Mail accounts
are still added with `python mail_digest.py add` in the Mail Digest folder.

## How it works

`app.py` runs a small web server that only this computer can reach (127.0.0.1) and opens it
in an Edge app window, which has no tabs or address bar. The server stops by itself about
10 minutes after you close the window. Opening the shortcut while it's still running
just opens another window.

To run it with a console showing progress:

```
python app.py
```
