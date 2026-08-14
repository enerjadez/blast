# BLAST

LAN file cannon. Send files between your PC and a phone/tablet over Wi‑Fi — no cloud, no account, no app store.

The PC runs a tiny local server. The other device opens a link (or scans a QR) in the browser.

## Run it

Windows:

```bat
blast.bat
```

or:

```bat
python blast.py
```

Share something the moment it starts:

```bat
python blast.py D:\Videos\movie.mkv
```

Leave the window open. Scan the QR from the tablet, or open the printed `http://192.168.x.x:7733/r/…/` link on the same Wi‑Fi.

Tablet uploads land in `~/blast-inbox`.

### Windows firewall (once)

If the tablet cannot open the page, run this in **Admin** PowerShell:

```powershell
netsh advfirewall firewall add rule name="BLAST LAN" dir=in action=allow protocol=TCP localport=7733 profile=private
```

## How it works

| Direction | What happens |
|---|---|
| **PC → tablet** | File stays on the PC. Tablet downloads it. *Share files / Share a folder* is a live view of that disk. |
| **Tablet → PC** | File is copied into the inbox folder. |
| **Save folder** | PC zips the folder (stored, no recompress) and the tablet saves `folder.zip`. |

Nothing leaves your LAN. The URL is your home IP.

## Tips

- Same 5/6 GHz Wi‑Fi on both devices. Not guest Wi‑Fi, not AP isolation.
- Big files from the PC: **Share files** (in-place). Don’t drag them through the PC browser.
- USB 3 is still faster for a single huge file. BLAST is for when you don’t want the cable.

## Requirements

Python 3.9+ (stdlib only). QR encoder is vendored from [Project Nayuki](https://www.nayuki.io/page/qr-code-generator-library) (MIT).
