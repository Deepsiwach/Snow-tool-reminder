# SNOW Reminder for Windows

SNOW Reminder is a background Windows app for checking the ServiceNow queue for
unassigned tickets. It runs in the notification area (including the hidden icons
area), has no taskbar window while idle, and starts automatically when you sign
in. Right-click its notification-area icon to open Settings, check SNOW now, or
exit the app. Choose **Uninstall** there to launch the Windows uninstaller; it
asks for confirmation and removes the app and its saved settings.

At the configured interval, a large borderless, topmost reminder window appears
and plays a continuous alert sound. It has no close button. The alert returns to
the foreground every minute until **OPEN SNOW** is clicked. Clicking it opens the
configured link, stops the sound, closes the alert, and starts the next interval.

## Install

Run `dist\SNOW Reminder Setup.exe` and follow the installer. It installs to the
current Windows user's profile and registers the app to run at sign-in. No Python
or other runtime needs to be installed. The installer also starts the app when
installation finishes. Open the app's notification-area menu and choose Settings
to set the SNOW URL and reminder interval.

On first launch, Settings opens automatically if no SNOW URL has been saved.
After saving, the app shows the confirmation message and returns to the
notification area when **OK** is clicked.

## Publish updates through GitHub

The updater checks for a newer GitHub Release shortly after launch and every 15
minutes afterward. When it finds one, it prompts the user, verifies the installer
download against GitHub's SHA-256 digest when available, and launches the setup
file. Installing the update keeps the existing SNOW settings. Users can also
choose **Check for updates** from the tray menu.

Set `GITHUB_REPOSITORY` in `update_config.py` to the public `OWNER/REPOSITORY`
that will host this source. The included GitHub Actions workflow builds and
publishes a new installer Release when code is pushed to `main` or `master`.
The repository must be public for installed copies to check and download releases
without a sign-in token. Push the project to GitHub and enable Actions; each
successful push publishes a versioned release automatically.

## Build from source

Install Python 3.10 or newer and Inno Setup 6, then run in PowerShell:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\build.ps1
```

The script downloads Python packaging dependencies, creates the standalone app,
and compiles the installer. Outputs are `dist\SNOW Reminder.exe` and
`dist\SNOW Reminder Setup.exe`.

Settings are stored in `%APPDATA%\SNOW Reminder\settings.json`. The app does not
connect to SNOW or determine ticket assignment; it opens the user's configured
link and serves as a reminder.
