# Putting the console on your home screen

The web console is a Progressive Web App, so Android can install it like
any other app: its own icon, its own window, no browser chrome. There is
no APK, no sideloading and no "restricted setting" dialog to clear.

## Install it

1. Start the agent in Termux:
   ```
   python -m android_agent
   ```
2. Open **Chrome** on the phone and go to `http://localhost:8765`.
3. Sign in once with your web token.
4. Chrome's menu (⋮) will offer **Install app** or **Add to Home screen**.
   Tap it.

An icon appears on your home screen. Opening it launches the console
full-screen with no address bar.

Firefox and Samsung Internet work too; only Chrome shows a dedicated
"Install app" entry, the others call it "Add to Home screen".

## Why this and not an APK

An APK would have to be sideloaded, cleared past Android's restricted
settings, and rebuilt and reinstalled every time the console changed. A
PWA updates the moment you `git pull` and restart the agent, because the
page is still served by the agent itself.

What it does not change: the agent must be running in Termux. The icon
opens a window onto the agent, it does not contain one.

## If "Install app" does not appear

Chrome only offers installation when every one of these holds, and stays
silent about which one failed:

| Requirement | Where it lives |
| --- | --- |
| A readable manifest | `/manifest.webmanifest`, served without a login |
| A 192px **and** a 512px icon, purpose `any` | `/static/icon-*.png` |
| `display: standalone` | the manifest |
| A service worker with a `fetch` handler | `/sw.js` |
| A secure context | `localhost` counts; a plain-http LAN address does not |

That last row is the usual cause. Reaching the console over your LAN
address, for example `http://192.168.1.5:8765`, is not a secure context,
so no install option is offered. Use `localhost` on the phone itself, or
put a tunnel with HTTPS in front of it.

## What the service worker stores

Only the page shell, so that opening the icon with Termux stopped shows
"Agent is not running" rather than a browser error. API responses are
never cached: the transcript, the file list and everything else private
goes straight to the network and is never written to disk by the browser.
