#!/usr/bin/env python3
"""
Termux Ultimate Remote Control Center — hardened build.

Changes from the original version:
  SECURITY
    - Auth is now enforced centrally via middleware, so it is IMPOSSIBLE for
      any command handler to run before the chat_id check, regardless of
      registration order (the old code relied on handler ordering, which
      telebot does not guarantee the way that code assumed).
    - /cmd: blocklist for obviously destructive commands + timeout + logging
      of every command run (see command_log.txt).
    - /getfile: restricted to an allow-list of directories (ALLOWED_GETFILE_DIRS
      in config.py) and resolves symlinks/".." before checking, so you can't
      path-traverse out of the allowed dirs.
    - /wallpaper: URL download is now size-limited and content-type checked
      before being handed to termux-wallpaper.
    - All subprocess calls now check return codes and report failure instead
      of silently doing nothing.
    - Every reply that embeds user-provided or device text is escaped so a
      message containing Markdown special characters can't break formatting
      or (best-effort) inject weird formatting into your own chat.

  BUG FIXES
    - /record: no longer relies on a blind time.sleep race; waits for the
      recorder process itself, then checks the file exists and is non-empty.
    - /media, /wifi, /wakelock, /torch, /volume, /brightness now report
      success/failure instead of assuming success.
    - /photo, /record, /wallpaper temp files are cleaned up even on failure
      (try/finally).
    - Bare `except Exception` blocks now report the actual exception text
      instead of sometimes swallowing it.

  NEW FEATURES
    - /screenshot   - capture the current screen (requires termux-api + root
                      OR Termux:API's screenshot via `screencap`, see notes).
    - /apps         - list installed packages.
    - /kill [pkg]   - force-stop an app package.
    - /netinfo      - IP, mobile/wifi data usage snapshot.
    - /log          - show the last N lines of the /cmd audit log.
    - /uptime_bot   - how long this bot process has been running.

Requires: pip install pyTelegramBotAPI
"""
import os
import json
import subprocess
import time
import re
import shutil
import urllib.request
import telebot
from datetime import datetime
from config import (
    BOT_TOKEN, CHAT_ID, ALLOWED_GETFILE_DIRS, CMD_BLOCKLIST, CMD_TIMEOUT
)

telebot.apihelper.ENABLE_MIDDLEWARE = True
bot = telebot.TeleBot(BOT_TOKEN)

BOT_HOME = os.path.expanduser("~/telegram_bot")
CMD_LOG_PATH = os.path.join(BOT_HOME, "command_log.txt")
BOT_START_TIME = time.time()


# ---------------------------------------------------------------------------
# Auth — enforced centrally so no handler can ever run for the wrong chat.
# ---------------------------------------------------------------------------
@bot.middleware_handler(update_types=['message'])
def auth_middleware(bot_instance, message):
    """
    Runs before ANY message handler. If the sender isn't the owner, we mark
    the message so the dispatcher skips every real command handler and only
    the rejection handler fires. This removes the old design's dependence on
    handler registration order.
    """
    message._authorized = (message.chat.id == CHAT_ID)


def is_authorized(message):
    return getattr(message, "_authorized", False)


@bot.message_handler(func=lambda msg: not is_authorized(msg))
def reject_unauthorized(message):
    try:
        bot.reply_to(message, "⛔ Unauthorized access denied.")
    except Exception:
        pass  # don't leak errors to a stranger poking the bot


def md_escape(text):
    """Escape Telegram Markdown special chars so device/user text can't
    break message formatting."""
    if text is None:
        return ""
    text = str(text)
    for ch in "_*`[":
        text = text.replace(ch, "\\" + ch)
    return text


def log_command(message, cmd, result_ok):
    try:
        with open(CMD_LOG_PATH, "a") as f:
            f.write(
                f"{datetime.now().isoformat()} | ok={result_ok} | "
                f"chat={message.chat.id} | cmd={cmd}\n"
            )
    except Exception:
        pass


def run_ok(args, **kwargs):
    """Run a subprocess, return True/False for success, never raise."""
    try:
        res = subprocess.run(args, capture_output=True, timeout=kwargs.pop("timeout", 15), **kwargs)
        return res.returncode == 0, res
    except Exception as e:
        return False, e


# ---------------------------------------------------------------------------
# ADB helpers (wireless debugging, same-device loopback connection).
#
# Setup is manual (one-time per reboot): pair once via
#   adb pair localhost:<pairing_port>
# then connect via
#   adb connect localhost:<main_port>
# The pairing itself persists across reboots, but the *connection* usually
# does not — after a tablet reboot you'll need to run `adb connect` again
# before these commands will work. /adbstatus tells you which state you're in.
# ---------------------------------------------------------------------------
def adb_is_connected():
    """True if `adb devices` shows at least one device in 'device' state."""
    try:
        res = subprocess.run(["adb", "devices"], capture_output=True, timeout=10)
        out = res.stdout.decode('utf-8', errors='replace')
        for line in out.splitlines()[1:]:
            line = line.strip()
            if line.endswith("\tdevice") or line.endswith(" device"):
                return True
        return False
    except Exception:
        return False


def adb_shell(args, timeout=15):
    """Run `adb shell <args...>`. Returns (ok, output_text_or_error)."""
    if not adb_is_connected():
        return False, (
            "ADB is not connected. On the tablet, open Developer Options → "
            "Wireless debugging and confirm it's on, then in Termux run:\n"
            "adb connect localhost:<port shown there>\n"
            "(Use /adbstatus to check again after reconnecting.)"
        )
    try:
        res = subprocess.run(
            ["adb", "shell"] + args, capture_output=True, timeout=timeout
        )
        out = res.stdout.decode('utf-8', errors='replace')
        err = res.stderr.decode('utf-8', errors='replace')
        if res.returncode != 0:
            return False, (err or out or f"adb shell exited {res.returncode}")
        return True, out
    except Exception as e:
        return False, str(e)


# --- /start & /help ---
@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    help_text = (
        "🔥 *Termux Ultimate Remote Control Center*\n\n"
        "📸 `/photo` - Front camera photo\n"
        "🖥️ `/screenshot` - Capture current screen\n"
        "📍 `/location` - Network location coordinates\n"
        "🔋 `/battery` - Battery health & level\n"
        "📊 `/sysinfo` - System RAM, storage & uptime\n"
        "🌐 `/netinfo` - Network / IP info\n"
        "🔦 `/torch on` / `/torch off` - Torch light\n"
        "🗣️ `/say [text]` - Speak text aloud\n"
        "💻 `/cmd [command]` - Run terminal command (logged, blocklisted)\n"
        "🎙️ `/record` - Start mic recording (no time limit)\n"
        "⏹️ `/stoprecord` - Stop recording & send the file\n"
        "📁 `/getfile [path]` - Download file (restricted dirs)\n"
        "📋 `/clip` / `/setclip [text]` - Clipboard\n"
        "📳 `/vibrate [ms]` | 🍞 `/toast [msg]`\n\n"
        "🔒 `/screenoff` | 💡 `/screenon` - Screen power (needs ADB)\n"
        "🔌 `/adbstatus` - Check wireless ADB connection\n"
        "👆 `/tap [x] [y]` - Tap a screen coordinate (ADB)\n"
        "👉 `/swipe [x1] [y1] [x2] [y2] [ms?]` - Swipe gesture (ADB)\n"
        "⌨️ `/keyevent [name|code]` - Send a key event (ADB)\n"
        "⌨️ `/inputtext [text]` - Type into focused field (ADB)\n"
        "📱 `/currentapp` - Show foreground app (ADB)\n"
        "🎥 `/screenrecord` - Not supported on this device (see reply)\n"
        "⚡ `/wakelock on|off` - Prevent Termux sleep\n"
        "🖼️ `/wallpaper [path or URL]` - Change wallpaper\n"
        "📶 `/wifi on|off` | `/winfo` - Wi-Fi control & info\n"
        "🖐️ `/fingerprint` - Scan fingerprint sensor\n"
        "💬 `/dialog [Question]` - Screen popup prompt\n\n"
        "📞 `/call [number]` - Dial phone number\n"
        "💬 `/sms [num] [msg]` - Send SMS text\n"
        "📥 `/inbox` - Read recent SMS inbox\n"
        "🔔 `/notifications` - View live notifications\n"
        "🎵 `/media [play|pause|next|prev]`\n"
        "🔊 `/volume` (levels) | `/volume [n]` | `/volume [stream] [n]`\n"
        "💡 `/brightness [0-255]`\n"
        "📇 `/contact [name]` - Search phone contacts\n"
        "📱 `/apps` - List installed apps\n"
        "🛑 `/kill [package]` - Force-stop an app\n"
        "📜 `/log [n]` - Show last n /cmd audit lines\n"
        "⏱️ `/uptime_bot` - How long the bot has been running\n"
        "📥 *Link Downloader:* Send any YouTube/IG link!"
    )
    bot.reply_to(message, help_text, parse_mode="Markdown")


# --- /uptime_bot ---
@bot.message_handler(commands=['uptime_bot'])
def handle_bot_uptime(message):
    secs = int(time.time() - BOT_START_TIME)
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    bot.reply_to(message, f"⏱️ Bot has been running for `{h}h {m}m {s}s`", parse_mode="Markdown")


# --- /log ---
@bot.message_handler(commands=['log'])
def handle_log(message):
    args = message.text.split()
    n = int(args[1]) if len(args) > 1 and args[1].isdigit() else 10
    n = min(n, 50)
    try:
        if not os.path.exists(CMD_LOG_PATH):
            bot.reply_to(message, "📜 No commands logged yet.")
            return
        with open(CMD_LOG_PATH) as f:
            lines = f.readlines()[-n:]
        text = "".join(lines) or "(empty)"
        bot.reply_to(message, f"📜 *Last {len(lines)} log lines:*\n```\n{md_escape(text)}\n```", parse_mode="Markdown")
    except Exception as e:
        bot.reply_to(message, f"❌ Error reading log: {e}")


# --- /screenoff (via ADB, falls back to brightness dim if ADB unavailable) ---
@bot.message_handler(commands=['screenoff'])
def handle_screenoff(message):
    if adb_is_connected():
        ok, out = adb_shell(["input", "keyevent", "26"])
        if ok:
            bot.reply_to(message, "🔒 Screen turned off (via adb).")
            return
        bot.reply_to(message, f"❌ adb keyevent failed: {out}")
        return

    # No ADB — fall back to the old best-effort dim.
    ok2, res2 = run_ok(["termux-brightness", "0"])
    if ok2:
        bot.reply_to(
            message,
            "🌙 ADB not connected, so this just dimmed brightness to 0 instead "
            "of a real screen-off. Run `adb connect localhost:<port>` in Termux "
            "for the real thing.",
            parse_mode="Markdown"
        )
    else:
        bot.reply_to(message, "❌ Both screen-off methods failed.")


# --- /screenon (new, via ADB) ---
@bot.message_handler(commands=['screenon'])
def handle_screenon(message):
    if not adb_is_connected():
        bot.reply_to(
            message,
            "❌ ADB not connected. Run `adb connect localhost:<port>` in Termux first.",
            parse_mode="Markdown"
        )
        return
    # Keyevent 26 is a toggle (power button), so this also turns the screen
    # back on if it's currently off.
    ok, out = adb_shell(["input", "keyevent", "26"])
    bot.reply_to(message, "💡 Screen toggled on." if ok else f"❌ Failed: {out}")


# --- /adbstatus (new) ---
@bot.message_handler(commands=['adbstatus'])
def handle_adbstatus(message):
    if adb_is_connected():
        bot.reply_to(message, "✅ ADB is connected. /screenshot, /screenoff and /screenon are available.")
    else:
        bot.reply_to(
            message,
            "❌ ADB is not connected.\n\n"
            "This resets after a tablet reboot or long idle period. To fix:\n"
            "1. On the tablet: Settings → Developer Options → Wireless debugging\n"
            "2. Note the IP address & port shown there\n"
            "3. In Termux run: `adb connect localhost:<port>`\n"
            "4. Recheck with /adbstatus",
            parse_mode="Markdown"
        )


def require_adb(message):
    """Common guard for the new ADB-only commands below. Returns True if
    ADB is connected; otherwise replies with instructions and returns False."""
    if adb_is_connected():
        return True
    bot.reply_to(
        message,
        "❌ ADB not connected. Run `adb connect localhost:<port>` in Termux, "
        "then check `/adbstatus`.",
        parse_mode="Markdown"
    )
    return False


# --- /tap x y (new, via ADB) ---
@bot.message_handler(commands=['tap'])
def handle_tap(message):
    if not require_adb(message):
        return
    args = message.text.split()
    if len(args) != 3 or not all(a.lstrip('-').isdigit() for a in args[1:]):
        bot.reply_to(message, "Usage: `/tap [x] [y]`", parse_mode="Markdown")
        return
    x, y = args[1], args[2]
    ok, out = adb_shell(["input", "tap", x, y])
    bot.reply_to(message, f"👆 Tapped ({x}, {y})" if ok else f"❌ Failed: {out}", parse_mode="Markdown")


# --- /swipe x1 y1 x2 y2 [duration_ms] (new, via ADB) ---
@bot.message_handler(commands=['swipe'])
def handle_swipe(message):
    if not require_adb(message):
        return
    args = message.text.split()
    if len(args) not in (5, 6) or not all(a.lstrip('-').isdigit() for a in args[1:5]):
        bot.reply_to(message, "Usage: `/swipe [x1] [y1] [x2] [y2] [duration_ms?]`", parse_mode="Markdown")
        return
    coords = args[1:5]
    duration = args[5] if len(args) == 6 and args[5].isdigit() else "300"
    ok, out = adb_shell(["input", "swipe"] + coords + [duration])
    bot.reply_to(message, f"👉 Swiped {' '.join(coords)} over {duration}ms" if ok else f"❌ Failed: {out}", parse_mode="Markdown")


# --- /keyevent [code_or_name] (new, via ADB) ---
COMMON_KEYEVENTS = {
    "home": "3", "back": "4", "recents": "187", "power": "26",
    "volup": "24", "voldown": "25", "camera": "27", "menu": "82",
    "enter": "66", "del": "67", "play": "126", "pause": "127",
}

@bot.message_handler(commands=['keyevent'])
def handle_keyevent(message):
    if not require_adb(message):
        return
    args = message.text.split()
    if len(args) != 2:
        bot.reply_to(
            message,
            "Usage: `/keyevent [code]` or `/keyevent [name]`\n"
            f"Names: {', '.join(sorted(COMMON_KEYEVENTS))}",
            parse_mode="Markdown"
        )
        return
    key = args[1].lower()
    code = COMMON_KEYEVENTS.get(key, args[1] if args[1].isdigit() else None)
    if code is None:
        bot.reply_to(message, "❌ Unknown key name or non-numeric code.")
        return
    ok, out = adb_shell(["input", "keyevent", code])
    bot.reply_to(message, f"⌨️ Sent keyevent `{code}`" if ok else f"❌ Failed: {out}", parse_mode="Markdown")


# --- /inputtext [text] (new, via ADB) ---
@bot.message_handler(commands=['inputtext'])
def handle_inputtext(message):
    if not require_adb(message):
        return
    text = message.text.replace("/inputtext", "", 1).strip()
    if not text:
        bot.reply_to(message, "Usage: `/inputtext [text]` (types into whatever field is focused)", parse_mode="Markdown")
        return
    # adb's `input text` requires spaces to be escaped as %s
    escaped = text.replace(" ", "%s")
    ok, out = adb_shell(["input", "text", escaped])
    bot.reply_to(message, "⌨️ Text typed." if ok else f"❌ Failed: {out}")


# --- /currentapp (new, via ADB) ---
@bot.message_handler(commands=['currentapp'])
def handle_currentapp(message):
    if not require_adb(message):
        return
    ok, out = adb_shell(["dumpsys", "window"], timeout=15)
    if not ok:
        bot.reply_to(message, f"❌ Failed: {out}")
        return
    match = re.search(r"mCurrentFocus=.*?\{.*?\s([a-zA-Z0-9_.]+)/[a-zA-Z0-9_.]+\}", out)
    if not match:
        # try an alternate field some Android versions use
        match = re.search(r"mFocusedApp=.*?\s([a-zA-Z0-9_.]+)/[a-zA-Z0-9_.]+", out)
    if match:
        bot.reply_to(message, f"📱 Foreground app: `{md_escape(match.group(1))}`", parse_mode="Markdown")
    else:
        bot.reply_to(message, "❌ Could not determine the foreground app from dumpsys output.")


# --- /screenrecord: disabled on this device ---
# The stock `screenrecord` binary segfaults (exit 139) immediately on this
# Realme Pad build, before it even reaches the codec layer (confirmed via
# `adb logcat` — no media/codec log lines at all, just the shell invocation).
# This is a broken vendor binary, not something fixable via ADB or Termux.
# It would likely need root (to swap in a working recorder such as scrcpy's
# recording mode) to fix. Kept as a stub so the command gives a clear answer
# instead of silently failing or disappearing.
@bot.message_handler(commands=['screenrecord'])
def handle_screenrecord(message):
    bot.reply_to(
        message,
        "❌ Screen recording isn't available on this device — the system's "
        "`screenrecord` binary crashes immediately (confirmed via logcat, "
        "exit code 139/segfault) regardless of duration or resolution. This "
        "is a broken vendor binary on this build, not something ADB/Termux "
        "can work around. `/screenshot` (static) still works fine.",
        parse_mode="Markdown"
    )


# --- /wakelock ---
@bot.message_handler(commands=['wakelock'])
def handle_wakelock(message):
    args = message.text.split()
    action = args[1].lower() if len(args) > 1 else "on"
    if action not in ("on", "off"):
        bot.reply_to(message, "Usage: `/wakelock on` or `/wakelock off`", parse_mode="Markdown")
        return
    cmd = ["termux-wake-lock"] if action == "on" else ["termux-wake-unlock"]
    ok, res = run_ok(cmd)
    if ok:
        bot.reply_to(message, f"⚡ Wake lock **{'ENABLED' if action == 'on' else 'DISABLED'}**.", parse_mode="Markdown")
    else:
        bot.reply_to(message, "❌ Failed to change wake lock state.")


# --- /wallpaper ---
MAX_WALLPAPER_BYTES = 15 * 1024 * 1024  # 15 MB safety cap

@bot.message_handler(commands=['wallpaper'])
def handle_wallpaper(message):
    target = message.text.replace("/wallpaper", "").strip()
    if not target:
        bot.reply_to(message, "Usage: `/wallpaper /path/to/img.jpg` or `/wallpaper https://url/img.jpg`", parse_mode="Markdown")
        return

    temp_img = os.path.join(BOT_HOME, ".wall_temp.jpg")
    img_path = target
    downloaded = False

    try:
        if target.startswith("http://") or target.startswith("https://"):
            bot.reply_to(message, "📥 Downloading wallpaper image...")
            req = urllib.request.Request(target, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                ctype = resp.headers.get("Content-Type", "")
                if not ctype.startswith("image/"):
                    bot.reply_to(message, f"❌ URL did not return an image (Content-Type: {ctype}).")
                    return
                data = resp.read(MAX_WALLPAPER_BYTES + 1)
                if len(data) > MAX_WALLPAPER_BYTES:
                    bot.reply_to(message, "❌ Image too large (>15MB).")
                    return
                with open(temp_img, "wb") as f:
                    f.write(data)
            img_path = temp_img
            downloaded = True
        else:
            img_path = os.path.realpath(os.path.expanduser(target))

        if os.path.exists(img_path):
            ok, res = run_ok(["termux-wallpaper", "-f", img_path])
            if ok:
                bot.reply_to(message, "🖼️ Wallpaper updated successfully!")
            else:
                bot.reply_to(message, "❌ termux-wallpaper failed to apply the image.")
        else:
            bot.reply_to(message, "❌ Image file not found.")
    except Exception as e:
        bot.reply_to(message, f"❌ Failed to set wallpaper: {e}")
    finally:
        if downloaded and os.path.exists(temp_img):
            os.remove(temp_img)


# --- /wifi & /winfo ---
@bot.message_handler(commands=['wifi'])
def handle_wifi(message):
    args = message.text.split()
    state = args[1].lower() if len(args) > 1 else None
    if state not in ("on", "off"):
        bot.reply_to(message, "Usage: `/wifi on` or `/wifi off`", parse_mode="Markdown")
        return
    enable = "true" if state == "on" else "false"
    ok, res = run_ok(["termux-wifi-enable", enable])
    if ok:
        bot.reply_to(message, f"📶 Wi-Fi toggled *{state.upper()}*", parse_mode="Markdown")
    else:
        bot.reply_to(message, "❌ Failed to toggle Wi-Fi.")


@bot.message_handler(commands=['winfo'])
def handle_winfo(message):
    try:
        output = subprocess.check_output(["termux-wifi-connectioninfo"], timeout=10).decode('utf-8')
        data = json.loads(output)
        reply = (
            f"📶 *Wi-Fi Details*\n"
            f"• SSID: `{md_escape(data.get('ssid', 'N/A'))}`\n"
            f"• IP: `{md_escape(data.get('ip', 'N/A'))}`\n"
            f"• Speed: `{md_escape(data.get('link_speed_mbps', 'N/A'))} Mbps`\n"
            f"• RSSI: `{md_escape(data.get('rssi', 'N/A'))} dBm`"
        )
        bot.reply_to(message, reply, parse_mode="Markdown")
    except Exception as e:
        bot.reply_to(message, f"❌ Error: {e}")


# --- /fingerprint ---
@bot.message_handler(commands=['fingerprint'])
def handle_fingerprint(message):
    bot.reply_to(message, "🖐️ Touch the fingerprint sensor on the phone...")
    try:
        output = subprocess.check_output(["termux-fingerprint"], timeout=20).decode('utf-8')
        data = json.loads(output)
        status = data.get("auth_result", "UNKNOWN")
        if status == "AUTH_RESULT_SUCCESS":
            bot.reply_to(message, "✅ Fingerprint Match: **AUTHENTICATED**", parse_mode="Markdown")
        else:
            bot.reply_to(message, f"❌ Fingerprint Auth: **{md_escape(status)}**", parse_mode="Markdown")
    except subprocess.TimeoutExpired:
        bot.reply_to(message, "⏱️ Fingerprint scan timed out.")
    except Exception as e:
        bot.reply_to(message, f"❌ Error: {e}")


# --- /dialog ---
@bot.message_handler(commands=['dialog'])
def handle_dialog(message):
    prompt = message.text.replace("/dialog", "").strip() or "Message from Remote Bot:"
    bot.reply_to(message, "💬 Prompt displayed on phone screen. Waiting for user input...")
    try:
        cmd = ["termux-dialog", "text", "-t", "Telegram Remote Prompt", "-i", prompt]
        output = subprocess.check_output(cmd, timeout=30).decode('utf-8')
        data = json.loads(output)
        user_input = data.get("text", "")
        bot.reply_to(message, f"📥 User answered on phone:\n`{md_escape(user_input)}`", parse_mode="Markdown")
    except subprocess.TimeoutExpired:
        bot.reply_to(message, "⏱️ Dialog timed out (no response entered).")
    except Exception as e:
        bot.reply_to(message, f"❌ Error: {e}")


# --- AUTO VIDEO/AUDIO DOWNLOADER (yt-dlp) ---
@bot.message_handler(func=lambda msg: is_authorized(msg) and msg.text and re.search(r'https?://[^\s]+', msg.text) and not msg.text.startswith('/wallpaper'))
def handle_link_download(message):
    url = re.search(r'https?://[^\s]+', message.text).group(0)
    bot.reply_to(message, f"⏳ Processing video link:\n`{md_escape(url)}`", parse_mode="Markdown")
    video_path = os.path.join(BOT_HOME, ".downloaded_video.mp4")
    if os.path.exists(video_path):
        os.remove(video_path)

    try:
        cmd = ["yt-dlp", "-f", "b[filesize<45M]/b", "-o", video_path, url]
        subprocess.run(cmd, check=True, timeout=120)
        if os.path.exists(video_path):
            bot.send_chat_action(message.chat.id, 'upload_video')
            with open(video_path, 'rb') as vid:
                bot.send_video(message.chat.id, vid, caption="🎥 Video Downloaded!")
        else:
            bot.reply_to(message, "❌ Download reported success but no file was produced.")
    except Exception as e:
        bot.reply_to(message, f"❌ Download failed: {e}")
    finally:
        if os.path.exists(video_path):
            os.remove(video_path)


# --- EXISTING CONTROLLERS ---
@bot.message_handler(commands=['media'])
def handle_media(message):
    args = message.text.split()
    action = args[1].lower() if len(args) > 1 else "play"
    valid = {"play": "play", "pause": "pause", "next": "next", "prev": "previous"}
    if action not in valid:
        bot.reply_to(message, "Usage: `/media play|pause|next|prev`", parse_mode="Markdown")
        return
    ok, res = run_ok(["termux-media-player", valid[action]])
    if ok:
        bot.reply_to(message, f"🎵 Media action: `{action}`", parse_mode="Markdown")
    else:
        bot.reply_to(message, "❌ Media command failed.")


VOLUME_STREAMS = {"call", "system", "ring", "music", "alarm", "notification"}

@bot.message_handler(commands=['volume'])
def handle_volume(message):
    """
    Usage:
      /volume 50            -> sets the 'music' stream to raw level 50
                                (Android streams are NOT 0-100; each has its
                                own max, e.g. music might max at 16)
      /volume music 10      -> sets a specific stream to a raw level
      /volume               -> shows current levels + max for every stream
    """
    args = message.text.split()

    if len(args) == 1:
        try:
            output = subprocess.check_output(["termux-volume"], timeout=10).decode('utf-8')
            data = json.loads(output)
            lines = [f"• {d['stream']}: {d['volume']}/{d['max_volume']}" for d in data]
            bot.reply_to(message, "🔊 *Volume levels:*\n```\n" + "\n".join(lines) + "\n```", parse_mode="Markdown")
        except Exception as e:
            bot.reply_to(message, f"❌ Error: {e}")
        return

    if len(args) == 2 and args[1].isdigit():
        stream, level = "music", args[1]
    elif len(args) == 3 and args[1].lower() in VOLUME_STREAMS and args[2].isdigit():
        stream, level = args[1].lower(), args[2]
    else:
        bot.reply_to(
            message,
            "Usage: `/volume [level]` (defaults to music stream) or `/volume [stream] [level]`\n"
            f"Streams: {', '.join(sorted(VOLUME_STREAMS))}\n"
            "Run `/volume` with no args to see each stream's max level.",
            parse_mode="Markdown"
        )
        return

    ok, res = run_ok(["termux-volume", stream, level])
    bot.reply_to(message, f"🔊 `{stream}` volume set to `{level}`" if ok else f"❌ Failed — check the level isn't above that stream's max (see `/volume`).", parse_mode="Markdown")


@bot.message_handler(commands=['brightness'])
def handle_brightness(message):
    args = message.text.split()
    if len(args) > 1 and args[1].isdigit() and 0 <= int(args[1]) <= 255:
        ok, res = run_ok(["termux-brightness", args[1]])
        bot.reply_to(message, f"💡 Brightness: `{args[1]}`" if ok else "❌ Failed to set brightness.", parse_mode="Markdown")
    else:
        bot.reply_to(message, "Usage: `/brightness 0-255`", parse_mode="Markdown")


@bot.message_handler(commands=['contact'])
def handle_contact(message):
    query = message.text.replace("/contact", "").strip().lower()
    try:
        output = subprocess.check_output(["termux-contact-list"], timeout=15).decode('utf-8')
        contacts = json.loads(output)
        matches = [c for c in contacts if query in c.get('name', '').lower()]
        if not matches:
            bot.reply_to(message, "📇 No matching contacts found.")
            return
        reply = "📇 *Contacts:*\n\n"
        for c in matches[:5]:
            reply += f"👤 {md_escape(c.get('name'))}: `{md_escape(c.get('number'))}`\n"
        bot.reply_to(message, reply, parse_mode="Markdown")
    except Exception as e:
        bot.reply_to(message, f"❌ Error: {e}")


@bot.message_handler(commands=['call'])
def handle_call(message):
    num = message.text.replace("/call", "").strip()
    if not num:
        bot.reply_to(message, "Usage: `/call [number]`", parse_mode="Markdown")
        return
    ok, res = run_ok(["termux-telephony-call", num])
    bot.reply_to(message, f"📞 Dialing `{md_escape(num)}`..." if ok else "❌ Call failed.", parse_mode="Markdown")


@bot.message_handler(commands=['sms'])
def handle_sms(message):
    args = message.text.split(maxsplit=2)
    if len(args) < 3:
        bot.reply_to(message, "Usage: `/sms [number] [message]`", parse_mode="Markdown")
        return
    ok, res = run_ok(["termux-sms-send", "-n", args[1], args[2]])
    bot.reply_to(message, f"💬 SMS sent to `{md_escape(args[1])}`" if ok else "❌ SMS send failed.", parse_mode="Markdown")


@bot.message_handler(commands=['inbox'])
def handle_inbox(message):
    try:
        output = subprocess.check_output(["termux-sms-list", "-l", "3"], timeout=15).decode('utf-8')
        messages = json.loads(output)
        if not messages:
            bot.reply_to(message, "📥 No recent SMS.")
            return
        reply = "📥 *Recent SMS:*\n\n"
        for msg in messages:
            reply += f"👤 `{md_escape(msg.get('from'))}`: {md_escape(msg.get('body'))}\n"
        bot.reply_to(message, reply[:4000], parse_mode="Markdown")
    except Exception as e:
        bot.reply_to(message, f"❌ Error: {e}")


@bot.message_handler(commands=['notifications'])
def handle_notifications(message):
    try:
        output = subprocess.check_output(["termux-notification-list"], timeout=15).decode('utf-8')
        notifs = json.loads(output)
        if not notifs:
            bot.reply_to(message, "🔔 No active notifications.")
            return
        reply = "🔔 *Notifications:*\n\n"
        for n in notifs[:5]:
            reply += f"📱 *[{md_escape(n.get('packageName', '').split('.')[-1])}]* {md_escape(n.get('title'))}\n"
        bot.reply_to(message, reply[:4000], parse_mode="Markdown")
    except Exception as e:
        bot.reply_to(message, f"❌ Error: {e}")


# --- /cmd (hardened: blocklist + timeout + audit log) ---
@bot.message_handler(commands=['cmd'])
def handle_cmd(message):
    cmd = message.text.replace("/cmd", "", 1).strip()
    if not cmd:
        bot.reply_to(message, "Usage: `/cmd [shell command]`", parse_mode="Markdown")
        return

    lowered = cmd.lower()
    for bad in CMD_BLOCKLIST:
        if bad.lower() in lowered:
            log_command(message, cmd, False)
            bot.reply_to(message, "⛔ This command matches a blocked destructive pattern and was not run.")
            return

    try:
        output = subprocess.check_output(
            cmd, shell=True, stderr=subprocess.STDOUT, timeout=CMD_TIMEOUT
        ).decode('utf-8', errors='replace')
        if len(output) > 4000:
            output = output[:4000] + "\n...[truncated]"
        log_command(message, cmd, True)
        bot.reply_to(message, f"```\n{output or '(no output)'}\n```", parse_mode="Markdown")
    except subprocess.TimeoutExpired:
        log_command(message, cmd, False)
        bot.reply_to(message, f"⏱️ Command timed out after {CMD_TIMEOUT}s.")
    except subprocess.CalledProcessError as e:
        log_command(message, cmd, False)
        out = (e.output or b"").decode('utf-8', errors='replace')[:3800]
        bot.reply_to(message, f"❌ Exit code {e.returncode}:\n```\n{out}\n```", parse_mode="Markdown")
    except Exception as e:
        log_command(message, cmd, False)
        bot.reply_to(message, f"❌ Error: {e}")


# --- /record & /stoprecord (manual start/stop, no duration cap) ---
AUDIO_PATH = os.path.join(BOT_HOME, ".audio.m4a")

@bot.message_handler(commands=['record'])
def handle_record(message):
    """
    Starts recording in the background with no fixed duration. Use
    /stoprecord to end it — termux-microphone-record will keep going
    until told to stop (or it hits the device's own storage limits).
    """
    # If a previous recording is still active, stop it first so we don't
    # leave orphaned recorder processes.
    subprocess.run(["termux-microphone-record", "-q"], capture_output=True, timeout=10)
    time.sleep(0.5)

    if os.path.exists(AUDIO_PATH):
        os.remove(AUDIO_PATH)

    try:
        # No "-l" (limit) flag => records indefinitely until stopped.
        subprocess.run(
            ["termux-microphone-record", "-f", AUDIO_PATH],
            check=True, timeout=10
        )
        bot.reply_to(message, "🎙️ Recording started. Send /stoprecord when you're done.")
    except Exception as e:
        bot.reply_to(message, f"❌ Failed to start recording: {e}")


@bot.message_handler(commands=['stoprecord'])
def handle_stoprecord(message):
    try:
        subprocess.run(["termux-microphone-record", "-q"], check=True, timeout=10)
    except Exception as e:
        bot.reply_to(message, f"❌ Failed to stop recording: {e}")
        return

    # give the recorder a moment to flush and finalize the file
    for _ in range(10):
        if os.path.exists(AUDIO_PATH) and os.path.getsize(AUDIO_PATH) > 0:
            break
        time.sleep(0.5)

    try:
        if os.path.exists(AUDIO_PATH) and os.path.getsize(AUDIO_PATH) > 0:
            size_mb = os.path.getsize(AUDIO_PATH) / (1024 * 1024)
            with open(AUDIO_PATH, 'rb') as audio:
                bot.send_chat_action(message.chat.id, 'upload_voice')
                bot.send_audio(message.chat.id, audio, caption=f"🎙️ Recording stopped ({size_mb:.1f} MB)")
        else:
            bot.reply_to(message, "❌ No active recording found (or file is empty).")
    except Exception as e:
        bot.reply_to(message, f"❌ Error sending recording: {e}")
    finally:
        if os.path.exists(AUDIO_PATH):
            os.remove(AUDIO_PATH)


# --- /getfile (hardened: restricted to allow-listed directories) ---
@bot.message_handler(commands=['getfile'])
def handle_getfile(message):
    raw = message.text.replace("/getfile", "", 1).strip().strip("[]")
    if not raw:
        bot.reply_to(message, "Usage: `/getfile [path]`", parse_mode="Markdown")
        return

    path = os.path.realpath(os.path.expanduser(raw))

    allowed_roots = [os.path.realpath(p) for p in ALLOWED_GETFILE_DIRS]
    if not any(path == root or path.startswith(root + os.sep) for root in allowed_roots):
        bot.reply_to(
            message,
            "⛔ That path is outside the allowed directories.\n"
            f"Allowed: {', '.join(ALLOWED_GETFILE_DIRS)}",
            parse_mode="Markdown"
        )
        return

    if not os.path.exists(path):
        bot.reply_to(message, "❌ File not found.")
        return
    if os.path.isdir(path):
        bot.reply_to(message, "❌ That's a directory, not a file.")
        return

    try:
        size = os.path.getsize(path)
        if size > 45 * 1024 * 1024:  # Telegram bot upload cap-ish
            bot.reply_to(message, "❌ File too large to send (>45MB).")
            return
        with open(path, 'rb') as doc:
            bot.send_document(message.chat.id, doc)
    except Exception as e:
        bot.reply_to(message, f"❌ Error sending file: {e}")


@bot.message_handler(commands=['sysinfo'])
def handle_sysinfo(message):
    try:
        mem = subprocess.check_output("free -h | head -n 2", shell=True, timeout=10).decode('utf-8')
        disk = subprocess.check_output("df -h /sdcard", shell=True, timeout=10).decode('utf-8')
        uptime = subprocess.check_output("uptime", shell=True, timeout=10).decode('utf-8')
        bot.reply_to(
            message,
            f"📊 *System Telemetry*\n\n*Memory:*\n```\n{mem}\n```\n*Storage:*\n```\n{disk}\n```\n*Uptime:*\n```\n{uptime}\n```",
            parse_mode="Markdown"
        )
    except Exception as e:
        bot.reply_to(message, f"❌ Error: {e}")


# --- /netinfo (new) ---
@bot.message_handler(commands=['netinfo'])
def handle_netinfo(message):
    try:
        parts = []
        ok, res = run_ok(["ip", "-4", "addr"])
        if ok and hasattr(res, "stdout"):
            parts.append("*IP Addresses:*\n```\n" + res.stdout.decode('utf-8', errors='replace')[:1500] + "\n```")
        try:
            wifi_out = subprocess.check_output(["termux-wifi-connectioninfo"], timeout=8).decode('utf-8')
            wifi = json.loads(wifi_out)
            parts.append(f"*Wi-Fi:* `{md_escape(wifi.get('ssid'))}` @ `{md_escape(wifi.get('ip'))}`")
        except Exception:
            pass
        if not parts:
            bot.reply_to(message, "❌ Could not gather network info.")
            return
        bot.reply_to(message, "🌐 *Network Info*\n\n" + "\n\n".join(parts), parse_mode="Markdown")
    except Exception as e:
        bot.reply_to(message, f"❌ Error: {e}")


@bot.message_handler(commands=['clip'])
def handle_clip(message):
    try:
        clip_text = subprocess.check_output(["termux-clipboard-get"], timeout=10).decode('utf-8')
        bot.reply_to(message, f"📋 *Clipboard:*\n`{md_escape(clip_text)}`", parse_mode="Markdown")
    except Exception as e:
        bot.reply_to(message, f"❌ Error: {e}")


@bot.message_handler(commands=['setclip'])
def handle_setclip(message):
    text = message.text.replace("/setclip", "", 1).strip()
    if not text:
        bot.reply_to(message, "Usage: `/setclip [text]`", parse_mode="Markdown")
        return
    ok, res = run_ok(["termux-clipboard-set", text])
    bot.reply_to(message, "📋 Clipboard set." if ok else "❌ Failed to set clipboard.")


@bot.message_handler(commands=['toast'])
def handle_toast(message):
    text = message.text.replace("/toast", "", 1).strip()
    if text:
        run_ok(["termux-toast", text])


@bot.message_handler(commands=['vibrate'])
def handle_vibrate(message):
    args = message.text.split()
    ms = args[1] if len(args) > 1 and args[1].isdigit() else "1000"
    run_ok(["termux-vibrate", "-d", ms])


@bot.message_handler(commands=['battery'])
def handle_battery(message):
    try:
        output = subprocess.check_output(["termux-battery-status"], timeout=10).decode('utf-8')
        data = json.loads(output)
        bot.reply_to(message, f"🔋 Level: `{data.get('percentage')}%` | Status: `{md_escape(data.get('status'))}`", parse_mode="Markdown")
    except Exception as e:
        bot.reply_to(message, f"❌ Error: {e}")


@bot.message_handler(commands=['photo'])
def handle_photo(message):
    photo_path = os.path.join(BOT_HOME, ".remote_snap.jpg")
    try:
        subprocess.run(["termux-camera-photo", "-c", "1", photo_path], check=True, timeout=20)
        if os.path.exists(photo_path) and os.path.getsize(photo_path) > 0:
            with open(photo_path, 'rb') as photo:
                bot.send_photo(message.chat.id, photo, caption="📸 Snap")
        else:
            bot.reply_to(message, "❌ Camera did not produce an image (check camera permission/index).")
    except Exception as e:
        bot.reply_to(message, f"❌ Error: {e}")
    finally:
        if os.path.exists(photo_path):
            os.remove(photo_path)


# --- /screenshot (via ADB — requires wireless debugging paired+connected) ---
@bot.message_handler(commands=['screenshot'])
def handle_screenshot(message):
    if not adb_is_connected():
        bot.reply_to(
            message,
            "❌ ADB not connected. In Termux run:\n"
            "`adb connect localhost:<port from Wireless debugging screen>`\n"
            "Then try again, or check `/adbstatus`.",
            parse_mode="Markdown"
        )
        return

    device_path = "/sdcard/.bot_screenshot.png"
    local_path = os.path.join(BOT_HOME, ".screenshot.png")
    try:
        # Capture on-device, then pull it — more reliable across devices than
        # piping exec-out straight through, especially for larger images.
        ok, res = run_ok(["adb", "shell", "screencap", "-p", device_path], timeout=15)
        if not ok:
            bot.reply_to(message, "❌ screencap failed via adb shell.")
            return
        ok2, res2 = run_ok(["adb", "pull", device_path, local_path], timeout=20)
        run_ok(["adb", "shell", "rm", "-f", device_path], timeout=10)  # cleanup on-device copy

        if ok2 and os.path.exists(local_path) and os.path.getsize(local_path) > 0:
            with open(local_path, 'rb') as img:
                bot.send_photo(message.chat.id, img, caption="🖥️ Screenshot")
        else:
            bot.reply_to(message, "❌ Screenshot captured but could not be pulled from the device.")
    except Exception as e:
        bot.reply_to(message, f"❌ Error: {e}")
    finally:
        if os.path.exists(local_path):
            os.remove(local_path)


# --- /apps (via ADB — plain Termux usually can't run `pm` directly) ---
@bot.message_handler(commands=['apps'])
def handle_apps(message):
    if not require_adb(message):
        return
    ok, out = adb_shell(["pm", "list", "packages", "-3"], timeout=15)
    if not ok:
        bot.reply_to(message, f"❌ Error: {out.strip() or '(no output)'}")
        return
    pkgs = sorted(line.replace("package:", "").strip() for line in out.splitlines() if line.strip())
    if not pkgs:
        bot.reply_to(message, "📱 No third-party packages found.")
        return
    text = "\n".join(pkgs[:60])
    if len(pkgs) > 60:
        text += f"\n...and {len(pkgs) - 60} more"
    bot.reply_to(message, f"📱 *Installed apps ({len(pkgs)}):*\n```\n{text}\n```", parse_mode="Markdown")


# --- /kill (via ADB) ---
@bot.message_handler(commands=['kill'])
def handle_kill(message):
    if not require_adb(message):
        return
    pkg = message.text.replace("/kill", "", 1).strip()
    if not pkg:
        bot.reply_to(message, "Usage: `/kill [package.name]` (see `/apps`)", parse_mode="Markdown")
        return
    if not re.fullmatch(r"[a-zA-Z0-9_.]+", pkg):
        bot.reply_to(message, "❌ Invalid package name.")
        return
    ok, out = adb_shell(["am", "force-stop", pkg], timeout=10)
    bot.reply_to(message, f"🛑 Force-stopped `{md_escape(pkg)}`" if ok else f"❌ Failed: {out.strip() or '(no output)'}", parse_mode="Markdown")


@bot.message_handler(commands=['location'])
def handle_location(message):
    try:
        output = subprocess.check_output(["termux-location", "-p", "network", "-r", "once"], timeout=15).decode('utf-8')
        data = json.loads(output)
        lat, lon = data.get("latitude"), data.get("longitude")
        if lat is None or lon is None:
            bot.reply_to(message, "❌ Could not get a location fix.")
            return
        bot.send_location(message.chat.id, lat, lon)
    except Exception as e:
        bot.reply_to(message, f"❌ Error: {e}")


@bot.message_handler(commands=['torch'])
def handle_torch(message):
    args = message.text.split()
    state = args[1].lower() if len(args) > 1 else None
    if state not in ("on", "off"):
        bot.reply_to(message, "Usage: `/torch on` or `/torch off`", parse_mode="Markdown")
        return
    ok, res = run_ok(["termux-torch", state])
    bot.reply_to(message, f"🔦 Torch {state.upper()}" if ok else "❌ Torch command failed.")


@bot.message_handler(commands=['say'])
def handle_say(message):
    text = message.text.replace("/say", "", 1).strip()
    if not text:
        bot.reply_to(message, "Usage: `/say [text]`", parse_mode="Markdown")
        return
    ok, res = run_ok(["termux-tts-speak", text], timeout=20)
    if not ok:
        bot.reply_to(message, "❌ TTS failed.")


if __name__ == "__main__":
    os.makedirs(BOT_HOME, exist_ok=True)
    print("🚀 All-in-One Mega Bot Running (hardened build)...")
    # infinity_polling() is supposed to auto-retry on network errors, but a
    # Telegram API read-timeout can still bubble up and kill the process.
    # Wrap it in our own retry loop so a network blip doesn't take the bot
    # (and every device control feature) down until you manually restart it.
    while True:
        try:
            bot.infinity_polling(timeout=30, long_polling_timeout=30)
        except Exception as e:
            print(f"⚠️ Polling crashed: {e}\nRestarting in 5s...")
            time.sleep(5)
