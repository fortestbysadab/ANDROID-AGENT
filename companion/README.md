# Android Agent Companion

A small Kotlin app that gives the agent a supported way to read the screen
and tap controls, replacing the ADB backend used in Phase 1.

It is built entirely on GitHub Actions. **No laptop and no local Android
SDK are required.**

## Build it

1. Copy `companion/ci/android-build.yml` to `.github/workflows/android-build.yml`.
   You can do this from a phone: on GitHub, **Add file → Create new file**,
   type that path, paste the contents, commit.
2. Go to **Actions → Build companion APK → Run workflow**.
3. When it finishes, download the **companion-debug-apk** artifact and
   install the APK.

The workflow uses the Android SDK that is already present on the runner
image. It deliberately avoids `android-actions/setup-android`, which now
fails because it calls `sdkmanager tools` for a package that no longer
exists.

## Pair it

1. Open the app and tap **Open Accessibility settings**, then enable
   **Android Agent**.
2. Back in the app, copy the `ANDROID_AGENT_COMPANION_TOKEN=...` line into
   the agent's `.env` in Termux and restart the agent.

## How the bridge is protected

The service listens on an abstract-namespace `LocalSocket`, which is the
same mechanism Termux:API uses, and which Python reaches through
`socket.AF_UNIX` with a leading NUL byte. Any app on the device can open
that address, so two checks guard it:

- every request carries the pairing token, compared without early exit;
- the first caller presenting a valid token pins its UID, and later
  connections must come from that same UID.

A leaked token alone therefore does not let a second app in, and guessing
the UID does not help without the token.

## What it will and will not do

The protocol is a fixed set of operations: `ping`, `dump`, `click`,
`long_click`, `set_text`, `scroll`, `global` (back/home/recents/
notifications) and `launch`. There is no arbitrary-gesture or
arbitrary-intent entry point. Element references are scoped to the dump
they came from, so a reference that outlives its screen is refused as
`stale_reference` rather than landing on whatever now occupies that slot.
Invisible nodes are dropped from dumps, text is capped at 500 characters,
and a dump is capped at 400 nodes.

The model never talks to any of this directly; it still only calls
`run_app_workflow` and `read_app_screen` with allowlisted workflow ids.

## Check it from Termux

```
python -m android_agent companion
```

This pings the bridge and then reads the current screen, printing the
foreground package and the first few labels it can see. It is the fastest
way to tell a pairing problem from an accessibility-toggle problem.

## Android 13 and later: "Restricted setting"

A sideloaded APK cannot be granted accessibility access from the normal
toggle; tapping it shows "For your security, this setting is currently
unavailable". Dismiss that dialog first, since seeing it is what reveals
the override, then go to the app's info page, open the three-dot menu and
choose **Allow restricted settings**. The accessibility toggle will then
work.
