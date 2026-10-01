# Daily Digest

Your mail and your news in one dark-mode window.

- **Mail** tab: unread mail from the last 14 days across all your accounts (Gmail, Outlook,
  Yahoo, iCloud or any IMAP server), sorted into "Needs attention", "From people" and
  newsletters. Inboxes are opened read-only, so nothing gets marked as read.
- **News** tab: gaming news from IGN, GameSpot, PC Gamer and others, then stock and crypto
  prices. No account or API key needed.

## Setup

You need [Python](https://www.python.org/downloads/) 3.9 or newer. On Windows, tick
"Add python.exe to PATH" in the installer.

1. Download this repository (green **Code** button > **Download ZIP**, then unzip it), or
   `git clone` it.
2. Open a terminal in that folder and install the two libraries the mail tab uses:

   ```
   pip install -r requirements.txt
   ```

3. Add your mail accounts, one at a time (skip this if you only want news):

   ```
   python mail_digest.py add
   ```

   It asks for your email address and then:
   - **Gmail, Yahoo, iCloud:** an *app password*, not your normal password. The prompt
     tells you where to create one. 2-step verification has to be on for that.
   - **Outlook / Hotmail / Live:** a one-time app registration first. See
     [Outlook setup](#outlook-setup) below.

4. Start the app:

   ```
   python app.py
   ```

   It opens in an Edge app window if Edge or Chrome is installed, otherwise in your
   normal browser.

To open it without a console window on Windows, make a desktop shortcut whose target is
`pythonw.exe "C:\path\to\daily-digest\app.py"` (use `icon.ico` from this folder as its icon).

## Using it

When the app opens it shows your last results straight away, then fetches fresh ones in the
background. **Refresh** (or F5) rebuilds the tab you're on. While the window is open, both tabs
refresh themselves every 30 minutes. When that happens, a "New updates" button appears instead
of the page jumping while you read.

Other mail commands:

```
python mail_digest.py list           # show your accounts
python mail_digest.py remove EMAIL   # remove an account and its saved sign-in
```

Both scripts also work on their own: `python mail_digest.py` and `python news_digest.py`
each build their page and open it in your browser. To change which news sites, stocks or
coins you follow, edit the lists at the top of `news_digest.py`.

## Outlook setup

Microsoft doesn't allow app passwords for personal Outlook accounts, so the mail tab signs in
through a free app registration of your own. You only do this once, even for several
Outlook accounts.

1. Go to the [Azure portal's App registrations](https://portal.azure.com/#view/Microsoft_AAD_RegisteredApps/ApplicationsListBlade)
   and sign in with any Microsoft account.
2. Click **New registration**. Give it any name (for example "Mail Digest"), choose
   **Personal Microsoft accounts only**, leave the redirect URI empty and click **Register**.
3. Open **Authentication**, set **Allow public client flows** to **Yes** and save.
4. Go back to **Overview** and copy the **Application (client) ID**.
5. Run `python mail_digest.py add`, enter your Outlook address and paste the ID when asked.
   It then shows a code and a link: open the link, enter the code and approve access.

The sign-in is remembered. If it ever expires, the mail tab says so; run `add` again for
that account (remove it first).

## Your passwords and data

Nothing private is stored in this folder, so it's safe to share or commit:

- App passwords go into your system's password store (Windows Credential Manager,
  macOS Keychain, or the Secret Service on Linux), never into a file.
- The account list and the Outlook sign-in cache are kept in `%APPDATA%\mail-digest` on
  Windows (`~/mail-digest` elsewhere).
- The pages the app builds, `digest.html` (your unread mail) and `news.html`, are written
  to this folder but listed in `.gitignore` so they're never committed.

## How it works

`app.py` runs a small web server that only this computer can reach (127.0.0.1) and opens it
in an app window, which has no tabs or address bar. It loads `mail_digest.py` and
`news_digest.py` from the same folder to build each tab. The server stops by itself about
10 minutes after you close the window. Opening the app while it's still running just opens
another window.
