"""
Config for the Termux Telegram remote control bot.

IMPORTANT:
- Keep this file private. Do not commit it to any public repo.
- Restrict its permissions:  chmod 600 config.py
- BOT_TOKEN grants full control of this bot / device — treat it like a password.
"""

BOT_TOKEN = "Telegram token"

# Your personal Telegram numeric chat ID (only this ID will be allowed to
# use the bot). Get it from @userinfobot on Telegram.
CHAT_ID = chat id here

# Directories /getfile is allowed to read from. Add more paths if you need,
# but keep this scoped — do NOT set it to "/".
ALLOWED_GETFILE_DIRS = [
    "/sdcard",
    "/data/data/com.termux/files/home",
]

# Substrings that will cause /cmd to refuse to run a command. Not bulletproof
# (this is a convenience guard, not a sandbox) but blocks the obvious
# destructive/self-sabotaging stuff run by accident or from a leaked token.
CMD_BLOCKLIST = [
    "rm -rf /",
    "rm -rf ~",
    "rm -rf /sdcard",
    ":(){ :|:& };:",   # fork bomb
    "mkfs",
    "dd if=",
    "> /dev/",
    "chmod -R 000",
]

# Max seconds a /cmd invocation may run before being killed.
CMD_TIMEOUT = 20
