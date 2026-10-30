"""Background SNOW reminder for Windows."""

from __future__ import annotations

import json
import hashlib
import os
import subprocess
import sys
import tempfile
import threading
import threading
import urllib.error
import urllib.request
import tkinter as tk
import webbrowser
import winsound
from pathlib import Path
from tkinter import messagebox, ttk

import pystray
from PIL import Image, ImageDraw
from update_config import GITHUB_REPOSITORY
from version import APP_VERSION

APP_NAME = "SNOW Reminder"
REPEAT_SECONDS = 60
DEFAULTS = {"snow_url": "", "interval_minutes": 60, "start_with_windows": True}
UPDATE_CHECK_MS = 15 * 60 * 1000
INSTALLER_ASSET = "SNOW Reminder Setup.exe"
MAX_INSTALLER_BYTES = 250 * 1024 * 1024


def config_path() -> Path:
    base = Path(os.environ.get("APPDATA", Path.home())) / APP_NAME
    base.mkdir(parents=True, exist_ok=True)
    return base / "settings.json"


def load_settings() -> dict:
    try:
        data = json.loads(config_path().read_text(encoding="utf-8"))
        return {**DEFAULTS, **data}
    except (OSError, ValueError, TypeError):
        return DEFAULTS.copy()


def save_settings(settings: dict) -> None:
    config_path().write_text(json.dumps(settings, indent=2), encoding="utf-8")


class SnowReminder:
    def __init__(self) -> None:
        self.settings = load_settings()
        self.root = tk.Tk()
        self.root.withdraw()
        self.root.title(APP_NAME)
        self.active_alert: tk.Toplevel | None = None
        self.alert_pending = False
        self.next_alert_id = None
        self.retry_id = None
        self.sound_active = False
        self.settings_window: tk.Toplevel | None = None
        self.update_check_active = False
        self.update_prompt_active = False
        self.dismissed_update_version: str | None = None
        self.pending_update: dict | None = None
        self.update_window: tk.Toplevel | None = None
        self.icon = pystray.Icon(APP_NAME, self.make_icon(), APP_NAME, menu=pystray.Menu(
            pystray.MenuItem("Settings", self.tray_settings, default=True),
            pystray.MenuItem("Check SNOW now", self.tray_alert),
            pystray.MenuItem("Check for updates", self.tray_check_updates),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Uninstall", self.tray_uninstall),
            pystray.MenuItem("Exit", self.tray_exit),
        ))

    @staticmethod
    def make_icon() -> Image.Image:
        image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle((5, 5, 59, 59), radius=12, fill="#b42318")
        draw.text((22, 10), "!", fill="white", stroke_width=1)
        return image

    def tray_settings(self, _icon=None, _item=None) -> None:
        self.root.after(0, self.show_settings)

    def tray_alert(self, _icon=None, _item=None) -> None:
        self.root.after(0, self.raise_alert)

    def tray_check_updates(self, _icon=None, _item=None) -> None:
        self.root.after(0, lambda: self.check_for_updates(manual=True))

    def tray_exit(self, _icon=None, _item=None) -> None:
        self.root.after(0, self.exit_app)

    def tray_uninstall(self, _icon=None, _item=None) -> None:
        self.root.after(0, self.uninstall_app)

    def uninstall_app(self) -> None:
        app_dir = Path(sys.executable if getattr(sys, "frozen", False) else sys.argv[0]).resolve().parent
        uninstaller = app_dir / "unins000.exe"
        if not uninstaller.is_file():
            messagebox.showerror(APP_NAME, "The uninstaller could not be found. Use Windows Installed apps to remove SNOW Reminder.")
            return
        if not messagebox.askyesno(APP_NAME, "Uninstall SNOW Reminder and remove its saved SNOW link and settings?"):
            return
        self.stop_sound()
        self.icon.stop()
        try:
            subprocess.Popen([str(uninstaller)])
        except OSError as exc:
            messagebox.showerror(APP_NAME, f"Could not start the uninstaller.\n\n{exc}")
            return
        self.root.destroy()

    def show_settings(self) -> None:
        if self.settings_window is not None and self.settings_window.winfo_exists():
            self.settings_window.deiconify()
            self.settings_window.lift()
            return
        window = tk.Toplevel(self.root)
        self.settings_window = window
        window.title(f"{APP_NAME} Settings")
        window.geometry("520x320")
        window.resizable(False, False)
        window.protocol("WM_DELETE_WINDOW", window.withdraw)
        frame = ttk.Frame(window, padding=22)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text=APP_NAME, font=("Segoe UI", 19, "bold")).pack(anchor="w")
        ttk.Label(frame, text="Set the link that opens your ServiceNow queue.", wraplength=450).pack(anchor="w", pady=(5, 16))
        ttk.Label(frame, text="SNOW link").pack(anchor="w")
        url_var = tk.StringVar(value=self.settings["snow_url"])
        ttk.Entry(frame, textvariable=url_var).pack(fill="x", pady=(4, 14))
        row = ttk.Frame(frame)
        row.pack(fill="x")
        ttk.Label(row, text="Remind again every").pack(side="left")
        interval_var = tk.StringVar(value=str(self.settings["interval_minutes"]))
        ttk.Spinbox(row, from_=1, to=10080, width=8, textvariable=interval_var).pack(side="left", padx=8)
        ttk.Label(row, text="minutes after opening SNOW").pack(side="left")
        ttk.Label(frame, text="The app starts automatically with Windows and stays in the notification area. An unanswered alert returns every minute with a continuous sound.", wraplength=465).pack(anchor="w", pady=(18, 14))

        def save() -> None:
            url = url_var.get().strip()
            try:
                interval = int(interval_var.get())
                if interval < 1:
                    raise ValueError
            except ValueError:
                messagebox.showerror(APP_NAME, "Enter an interval of at least 1 minute.", parent=window)
                return
            if not url.lower().startswith(("https://", "http://")):
                messagebox.showerror(APP_NAME, "Enter a SNOW URL beginning with https:// or http://.", parent=window)
                return
            self.settings = {"snow_url": url, "interval_minutes": interval, "start_with_windows": True}
            save_settings(self.settings)
            if not self.alert_pending:
                self.schedule_next()
            messagebox.showinfo(APP_NAME, "Settings saved.", parent=window)

        ttk.Button(frame, text="Save settings", command=save).pack(anchor="w")

    @staticmethod
    def version_key(version: str) -> tuple[int, ...]:
        import re
        parts = re.findall(r"\d+", version)
        if not parts:
            raise ValueError("The release tag does not contain a numeric version.")
        return tuple(int(part) for part in parts)

    def check_for_updates(self, manual: bool = False) -> None:
        if self.update_check_active:
            if manual:
                messagebox.showinfo(APP_NAME, "An update check is already running.")
            return
        if GITHUB_REPOSITORY == "OWNER/REPOSITORY" or "/" not in GITHUB_REPOSITORY:
            if manual:
                messagebox.showinfo(APP_NAME, "Update checks will be available after the GitHub repository is configured.")
            return
        self.update_check_active = True
        threading.Thread(target=self._fetch_latest_release, args=(manual,), daemon=True,
                         name="SNOW update check").start()

    def _fetch_latest_release(self, manual: bool) -> None:
        try:
            url = f"https://api.github.com/repos/{GITHUB_REPOSITORY}/releases/latest"
            request = urllib.request.Request(url, headers={
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "SNOW-Reminder-Updater",
            })
            with urllib.request.urlopen(request, timeout=15) as response:
                release = json.loads(response.read().decode("utf-8"))
            remote_version = str(release.get("tag_name", "")).lstrip("vV")
            if self.version_key(remote_version) <= self.version_key(APP_VERSION):
                self.root.after(0, lambda: self._update_check_finished(manual, None))
                return
            asset = next((item for item in release.get("assets", [])
                          if item.get("name") == INSTALLER_ASSET), None)
            if not asset or not str(asset.get("browser_download_url", "")).startswith("https://github.com/"):
                raise ValueError(f"Release {remote_version} does not contain {INSTALLER_ASSET}.")
            info = {"version": remote_version, "url": asset["browser_download_url"],
                    "digest": asset.get("digest"), "size": int(asset.get("size", 0))}
            if info["size"] > MAX_INSTALLER_BYTES:
                raise ValueError("The installer is larger than the allowed download size.")
            self.root.after(0, lambda: self._update_check_finished(manual, info))
        except Exception as exc:
            self.root.after(0, lambda error=str(exc): self._update_check_finished(manual, None, error))

    def _update_check_finished(self, manual: bool, info: dict | None, error: str | None = None) -> None:
        self.update_check_active = False
        if error:
            if manual:
                messagebox.showerror(APP_NAME, f"Could not check for updates.\n\n{error}")
            return
        if info is None:
            if manual:
                messagebox.showinfo(APP_NAME, f"SNOW Reminder is up to date (v{APP_VERSION}).")
            return
        if self.dismissed_update_version == info["version"]:
            if manual:
                self.dismissed_update_version = None
            else:
                return
        if self.update_prompt_active:
            return
        self.pending_update = info
        self.update_prompt_active = True
        if messagebox.askyesno(APP_NAME,
                               f"SNOW Reminder v{info['version']} is available. Install it now?\n\n"
                               "The app will close while the update installs."):
            self.start_update_download(info)
        else:
            self.dismissed_update_version = info["version"]
            self.update_prompt_active = False

    def start_update_download(self, info: dict) -> None:
        window = tk.Toplevel(self.root)
        self.update_window = window
        window.title("Updating SNOW Reminder")
        window.geometry("360x120")
        window.resizable(False, False)
        window.attributes("-topmost", True)
        ttk.Label(window, text=f"Downloading version {info['version']}…", padding=(18, 18, 18, 8)).pack(anchor="w")
        ttk.Progressbar(window, mode="indeterminate").pack(fill="x", padx=18, pady=(0, 16))
        window.grab_set()
        threading.Thread(target=self._download_update, args=(info,), daemon=True,
                         name="SNOW update download").start()

    def _download_update(self, info: dict) -> None:
        target = Path(tempfile.gettempdir()) / f"SNOW-Reminder-Setup-{info['version']}.exe"
        digest = hashlib.sha256()
        downloaded = 0
        try:
            request = urllib.request.Request(info["url"], headers={"User-Agent": "SNOW-Reminder-Updater"})
            with urllib.request.urlopen(request, timeout=30) as response, target.open("wb") as output:
                while True:
                    block = response.read(1024 * 1024)
                    if not block:
                        break
                    downloaded += len(block)
                    if downloaded > MAX_INSTALLER_BYTES:
                        raise ValueError("The installer download exceeded the allowed size.")
                    digest.update(block)
                    output.write(block)
            expected = info.get("digest")
            if expected and expected.startswith("sha256:") and digest.hexdigest().lower() != expected[7:].lower():
                raise ValueError("The downloaded installer did not match GitHub's SHA-256 digest.")
            if info.get("size") and downloaded != info["size"]:
                raise ValueError("The downloaded installer size did not match the published release asset.")
            self.root.after(0, lambda: self._launch_update(target))
        except Exception as exc:
            target.unlink(missing_ok=True)
            self.root.after(0, lambda error=str(exc): self._update_download_failed(error))

    def _update_download_failed(self, error: str) -> None:
        self._close_update_window()
        self.update_prompt_active = False
        messagebox.showerror(APP_NAME, f"Could not download the update.\n\n{error}")

    def _close_update_window(self) -> None:
        if self.update_window is not None and self.update_window.winfo_exists():
            self.update_window.grab_release()
            self.update_window.destroy()
        self.update_window = None

    def _launch_update(self, installer: Path) -> None:
        self._close_update_window()
        try:
            subprocess.Popen([str(installer)])
        except OSError as exc:
            self.update_prompt_active = False
            messagebox.showerror(APP_NAME, f"Could not launch the installer.\n\n{exc}")
            return
        self.stop_sound()
        self.icon.stop()
        self.root.destroy()

    def schedule_next(self) -> None:
        if self.next_alert_id is not None:
            self.root.after_cancel(self.next_alert_id)
        delay = max(1, int(self.settings["interval_minutes"])) * 60 * 1000
        self.next_alert_id = self.root.after(delay, self.raise_alert)

    def raise_alert(self) -> None:
        self.next_alert_id = None
        if not self.settings.get("snow_url"):
            self.show_settings()
            return
        self.alert_pending = True
        self.show_alert()

    def show_alert(self) -> None:
        if not self.alert_pending:
            return
        if self.active_alert is None or not self.active_alert.winfo_exists():
            popup = tk.Toplevel(self.root)
            self.active_alert = popup
            popup.overrideredirect(True)
            popup.protocol("WM_DELETE_WINDOW", lambda: popup.lift())
            popup.attributes("-topmost", True)
            popup.configure(bg="#9a3412")
            width, height = 560, 290
            x = max(0, (popup.winfo_screenwidth() - width) // 2)
            y = max(0, (popup.winfo_screenheight() - height) // 2)
            popup.geometry(f"{width}x{height}+{x}+{y}")
            body = tk.Frame(popup, bg="#fff7ed", padx=32, pady=27)
            body.pack(fill="both", expand=True, padx=5, pady=5)
            tk.Label(body, text="ACTION REQUIRED", bg="#fff7ed", fg="#9a3412",
                     font=("Segoe UI", 21, "bold")).pack(anchor="w")
            tk.Label(body, text="Check SNOW for unassigned tickets now.", bg="#fff7ed", fg="#1f2937",
                     font=("Segoe UI", 15), wraplength=480).pack(anchor="w", pady=(18, 24))
            open_button = tk.Button(body, text="OPEN SNOW", command=self.open_snow,
                                    bg="#b42318", fg="white", activebackground="#912018",
                                    activeforeground="white", font=("Segoe UI", 13, "bold"),
                                    relief="flat", padx=20, pady=12, cursor="hand2")
            open_button.pack(anchor="w")
            tk.Label(body, text="This alert stays open and sounds until SNOW is opened.",
                     bg="#fff7ed", fg="#52606d", font=("Segoe UI", 10)).pack(anchor="w", pady=(19, 0))
        self.active_alert.lift()
        self.active_alert.attributes("-topmost", True)
        self.start_sound()
        if self.retry_id is not None:
            self.root.after_cancel(self.retry_id)
        self.retry_id = self.root.after(REPEAT_SECONDS * 1000, self.repeat_alert)

    def start_sound(self) -> None:
        if self.sound_active:
            return
        self.sound_active = True
        try:
            winsound.PlaySound("SystemExclamation", winsound.SND_ALIAS | winsound.SND_LOOP | winsound.SND_ASYNC)
        except RuntimeError:
            winsound.Beep(1400, 800)

    def stop_sound(self) -> None:
        self.sound_active = False
        winsound.PlaySound(None, 0)

    def repeat_alert(self) -> None:
        self.retry_id = None
        if self.alert_pending:
            self.show_alert()

    def open_snow(self) -> None:
        try:
            if not webbrowser.open(self.settings["snow_url"], new=2):
                raise RuntimeError("Windows did not open the link.")
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"Could not open the SNOW link.\n\n{exc}")
            return
        self.alert_pending = False
        self.stop_sound()
        if self.retry_id is not None:
            self.root.after_cancel(self.retry_id)
            self.retry_id = None
        if self.active_alert is not None:
            self.active_alert.destroy()
            self.active_alert = None
        self.schedule_next()

    def exit_app(self) -> None:
        self.stop_sound()
        self.icon.stop()
        self.root.destroy()

    def run(self) -> None:
        threading.Thread(target=self.icon.run, daemon=True, name="SNOW notification icon").start()
        self.schedule_next()
        self.root.after(15_000, self.check_for_updates)
        self.root.after(UPDATE_CHECK_MS, self._schedule_update_check)
        self.root.mainloop()

    def _schedule_update_check(self) -> None:
        self.check_for_updates()
        self.root.after(UPDATE_CHECK_MS, self._schedule_update_check)


if __name__ == "__main__":
    SnowReminder().run()
