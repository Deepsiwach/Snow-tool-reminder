"""Background SNOW reminder for Windows."""

from __future__ import annotations

import json
import hashlib
import logging
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import tkinter as tk
import webbrowser
import winsound
from pathlib import Path
from tkinter import messagebox

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
NO_RELEASE_MESSAGE = "Already up to date. Enjoy Buddy."


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
        logging.basicConfig(
            filename=str(config_path().with_name("app.log")),
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(threadName)s %(message)s",
            encoding="utf-8",
        )
        self.root = tk.Tk()
        self.root.withdraw()
        self.root.title(APP_NAME)
        self.root.report_callback_exception = self._report_callback_exception
        self.active_alert: tk.Toplevel | None = None
        self.alert_pending = False
        self.next_alert_id = None
        self.next_alert_at: float | None = None
        self.retry_id = None
        self.sound_active = False
        self.settings_window: tk.Toplevel | None = None
        self.update_check_active = False
        self.update_prompt_active = False
        self.dismissed_update_version: str | None = None
        self.pending_update: dict | None = None
        self.update_window: tk.Toplevel | None = None
        self.update_progress_canvas: tk.Canvas | None = None
        self.update_progress_fill: int | None = None
        self.update_progress_text: tk.Label | None = None
        self.update_progress_detail: tk.Label | None = None
        self.tray_thread: threading.Thread | None = None
        self.shutting_down = False
        self.icon = self.make_tray_icon()
        logging.info("Started version %s", APP_VERSION)

    def make_tray_icon(self) -> pystray.Icon:
        return pystray.Icon(APP_NAME, self.make_icon(1.0), APP_NAME, menu=pystray.Menu(
            pystray.MenuItem("Settings", self.tray_settings, default=True),
            pystray.MenuItem("Check SNOW now", self.tray_alert),
            pystray.MenuItem("Check for updates", self.tray_check_updates),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Uninstall", self.tray_uninstall),
            pystray.MenuItem("Exit", self.tray_exit),
        ))

    @staticmethod
    def _report_callback_exception(exc_type, exc_value, traceback) -> None:
        logging.error("Unhandled Tk callback error", exc_info=(exc_type, exc_value, traceback))

    def _run_tray_icon(self) -> None:
        try:
            self.icon.run()
        except Exception:
            logging.exception("Tray icon loop stopped unexpectedly")

    def _start_tray_icon(self) -> None:
        self.tray_thread = threading.Thread(
            target=self._run_tray_icon, daemon=False, name="SNOW notification icon")
        self.tray_thread.start()

    @staticmethod
    def make_icon(progress: float = 1.0) -> Image.Image:
        """Draw a small timer dial whose colored arc drains toward the next alert."""
        image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        draw.ellipse((5, 5, 59, 59), fill="#172b4d")
        draw.ellipse((10, 10, 54, 54), outline="#42536d", width=5)
        sweep = max(0, min(360, int(360 * progress)))
        if sweep:
            draw.arc((10, 10, 54, 54), start=-90, end=-90 + sweep,
                     fill="#35d0ba", width=5)
        draw.ellipse((29, 29, 35, 35), fill="#f4f7fb")
        draw.line((32, 32, 32, 19), fill="#f4f7fb", width=3)
        draw.line((32, 32, 41, 36), fill="#f4f7fb", width=3)
        return image

    def _refresh_tray_icon(self) -> None:
        if self.next_alert_at is None:
            progress = 0.0 if self.alert_pending else 1.0
        else:
            total = max(1.0, float(self.settings.get("interval_minutes", 60)) * 60)
            progress = max(0.0, min(1.0, (self.next_alert_at - time.monotonic()) / total))
        try:
            self.icon.icon = self.make_icon(progress)
            minutes = max(0, int((self.next_alert_at - time.monotonic()) / 60)) if self.next_alert_at else 0
            self.icon.title = f"{APP_NAME} — alert in about {minutes} min" if self.next_alert_at else APP_NAME
        except Exception:
            pass

    def _tick_tray_icon(self) -> None:
        if (not self.shutting_down
                and (self.tray_thread is None or not self.tray_thread.is_alive())):
            logging.warning("Tray thread is not running; recreating the notification icon")
            self.icon = self.make_tray_icon()
            self._start_tray_icon()
        self._refresh_tray_icon()
        self.root.after(5000, self._tick_tray_icon)

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
        self.shutting_down = True
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
        window.title(f"{APP_NAME}  |  Preferences")
        window.geometry("600x580")
        window.resizable(False, False)
        window.protocol("WM_DELETE_WINDOW", window.withdraw)
        window.configure(bg="#f3f6fb")
        window.update_idletasks()
        sw, sh = window.winfo_screenwidth(), window.winfo_screenheight()
        window.geometry(f"600x580+{max(0, (sw - 600)//2)}+{max(0, (sh - 580)//2)}")
        bg, card, ink, muted, accent = "#f3f6fb", "#ffffff", "#172b4d", "#66758a", "#087f8c"
        shell = tk.Frame(window, bg=bg)
        shell.pack(fill="both", expand=True, padx=24, pady=22)
        header = tk.Frame(shell, bg=bg)
        header.pack(fill="x", pady=(0, 18))
        tk.Label(header, text="SNOW", bg=bg, fg=accent,
                 font=("Segoe UI", 11, "bold")).pack(anchor="w")
        tk.Label(header, text="Reminder settings", bg=bg, fg=ink,
                 font=("Segoe UI", 22, "bold")).pack(anchor="w", pady=(3, 1))
        tk.Label(header, text="A little nudge to keep your queue on track.", bg=bg, fg=muted,
                 font=("Segoe UI", 10)).pack(anchor="w")
        footer = tk.Frame(shell, bg=bg)
        footer.pack(side="bottom", fill="x", pady=(12, 0))
        frame = tk.Frame(shell, bg=card, highlightthickness=1, highlightbackground="#e3e9f2")
        frame.pack(fill="both", expand=True)
        content = tk.Frame(frame, bg=card)
        content.pack(fill="both", expand=True, padx=22, pady=21)
        tk.Label(content, text="ServiceNow queue", bg=card, fg=ink,
                 font=("Segoe UI", 12, "bold")).pack(anchor="w")
        tk.Label(content, text="Paste the page you want to open when a reminder appears.",
                 bg=card, fg=muted, wraplength=500, justify="left").pack(anchor="w", pady=(4, 13))
        tk.Label(content, text="SNOW link", bg=card, fg=ink,
                 font=("Segoe UI", 9, "bold")).pack(anchor="w")
        url_var = tk.StringVar(value=self.settings["snow_url"])
        url_entry = tk.Entry(content, textvariable=url_var, relief="flat", bg="#f7f9fc", fg=ink,
                             insertbackground=ink, highlightthickness=1,
                             highlightbackground="#d9e1ec", highlightcolor=accent,
                             font=("Segoe UI", 10))
        url_entry.pack(fill="x", ipady=10, pady=(6, 19))
        tk.Label(content, text="REMINDER INTERVAL", bg=card, fg=muted,
                 font=("Segoe UI", 8, "bold")).pack(anchor="w")
        row = tk.Frame(content, bg=card)
        row.pack(fill="x", pady=(8, 0))
        interval_var = tk.StringVar(value=str(self.settings["interval_minutes"]))
        tk.Spinbox(row, from_=1, to=10080, width=8, textvariable=interval_var,
                   relief="flat", bg="#f7f9fc", fg=ink, buttonbackground="#e8eef5",
                   highlightthickness=1, highlightbackground="#d9e1ec",
                   font=("Segoe UI", 11)).pack(side="left", ipady=5)
        tk.Label(row, text="minutes between reminders", bg=card, fg=ink,
                 font=("Segoe UI", 10)).pack(side="left", padx=11)
        note = tk.Frame(content, bg="#eef7f7")
        note.pack(fill="x", pady=(22, 0))
        tk.Label(note, text="TIMER", bg="#eef7f7", fg=accent,
                 font=("Segoe UI", 9, "bold")).pack(side="left", padx=(12, 9), pady=8)
        tk.Label(note, text="The tray timer drains as your next reminder gets closer.\n"
                 "Unanswered alerts return every minute with a sound.", bg="#eef7f7", fg="#35505f",
                 justify="left", font=("Segoe UI", 9)).pack(side="left", pady=9)
        tk.Label(footer, text="Runs quietly from the notification area", bg=bg, fg=muted,
                 font=("Segoe UI", 9)).pack(side="left", pady=10)

        def save() -> None:
            url = url_var.get().strip()
            try:
                interval = int(interval_var.get())
                if interval < 1:
                    raise ValueError
            except ValueError:
                messagebox.showerror(APP_NAME, "Enter an interval of at least 1 minute.", parent=window)
                return
            parsed_url = urllib.parse.urlsplit(url)
            if (parsed_url.scheme.lower() != "https" or not parsed_url.hostname
                    or parsed_url.username or parsed_url.password
                    or any(ord(char) < 32 for char in url)):
                messagebox.showerror(APP_NAME, "Enter a valid SNOW link beginning with https://.", parent=window)
                return
            self.settings = {"snow_url": url, "interval_minutes": interval, "start_with_windows": True}
            save_settings(self.settings)
            if not self.alert_pending:
                self.schedule_next()
            messagebox.showinfo(
                APP_NAME,
                "Don't Worry Buddy I am here to remind you now for SNOW, we will not let any response breach",
                parent=window,
            )
            if window.winfo_exists():
                window.destroy()
            self.settings_window = None
            self.root.after(1_000, self.check_for_updates)

        save_button = tk.Button(footer, text="Save settings", command=save, bg=accent, fg="white",
                                activebackground="#066b75", activeforeground="white", relief="flat",
                                bd=0, padx=22, pady=10, cursor="hand2",
                                font=("Segoe UI", 10, "bold"))
        save_button.pack(side="right")

    @staticmethod
    def version_key(version: str) -> tuple[int, ...]:
        import re
        parts = re.findall(r"\d+", version)
        if not parts:
            raise ValueError("The release tag does not contain a numeric version.")
        return tuple(int(part) for part in parts)

    def check_for_updates(self, manual: bool = False) -> None:
        if not manual and not self.settings.get("snow_url", "").strip():
            return
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
            # GitHub asset names may arrive with spaces converted to dots by
            # the release publishing path. Compare the meaningful name parts
            # while still requiring the expected Windows installer filename.
            expected_asset_name = re.sub(r"[^a-z0-9]", "", INSTALLER_ASSET.casefold())
            asset = next((item for item in release.get("assets", [])
                          if re.sub(r"[^a-z0-9]", "", str(item.get("name", "")).casefold())
                          == expected_asset_name), None)
            if not asset or not str(asset.get("browser_download_url", "")).startswith("https://github.com/"):
                raise ValueError(
                    f"GitHub release v{remote_version} has no installer attached yet. "
                    "The release workflow must upload the setup file."
                )
            info = {"version": remote_version, "url": asset["browser_download_url"],
                    "digest": asset.get("digest"), "size": int(asset.get("size", 0))}
            if not str(info["digest"] or "").startswith("sha256:"):
                raise ValueError("GitHub did not provide a SHA-256 checksum for this installer.")
            if info["size"] > MAX_INSTALLER_BYTES:
                raise ValueError("The installer is larger than the allowed download size.")
            self.root.after(0, lambda: self._update_check_finished(manual, info))
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                error = NO_RELEASE_MESSAGE
            else:
                error = str(exc)
            self.root.after(0, lambda message=error: self._update_check_finished(manual, None, message))
        except Exception as exc:
            self.root.after(0, lambda error=str(exc): self._update_check_finished(manual, None, error))

    def _update_check_finished(self, manual: bool, info: dict | None, error: str | None = None) -> None:
        self.update_check_active = False
        if error:
            if manual:
                if error == NO_RELEASE_MESSAGE:
                    messagebox.showinfo(APP_NAME, error)
                else:
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
        width, height = 520, 278
        screen_width, screen_height = window.winfo_screenwidth(), window.winfo_screenheight()
        window.geometry(f"{width}x{height}+{max(0, (screen_width-width)//2)}+{max(0, (screen_height-height)//2)}")
        window.resizable(False, False)
        window.attributes("-topmost", True)
        window.configure(bg="#edf2f7")
        card = tk.Frame(window, bg="#ffffff", highlightthickness=1, highlightbackground="#dce5ef")
        card.pack(fill="both", expand=True, padx=18, pady=18)
        header = tk.Frame(card, bg="#14304a", height=66)
        header.pack(fill="x")
        header.pack_propagate(False)
        tk.Label(header, text="SNOW  /  SOFTWARE UPDATE", bg="#14304a", fg="#87e2d3",
                 font=("Segoe UI", 9, "bold")).pack(anchor="w", padx=22, pady=(11, 1))
        tk.Label(header, text=f"Preparing version {info['version']}", bg="#14304a", fg="#ffffff",
                 font=("Segoe UI", 16, "bold")).pack(anchor="w", padx=22)
        body = tk.Frame(card, bg="#ffffff")
        body.pack(fill="both", expand=True, padx=22, pady=(18, 14))
        tk.Label(body, text="Downloading the latest reminder", bg="#ffffff", fg="#20364b",
                 font=("Segoe UI", 11, "bold")).pack(anchor="w")
        tk.Label(body, text="Keep this window open while we securely prepare the installer.",
                 bg="#ffffff", fg="#697b8c", font=("Segoe UI", 9)).pack(anchor="w", pady=(4, 14))
        self.update_progress_canvas = tk.Canvas(body, height=14, bg="#e6edf3", bd=0,
                                                highlightthickness=0, relief="flat")
        self.update_progress_canvas.pack(fill="x")
        self.update_progress_fill = self.update_progress_canvas.create_rectangle(
            0, 0, 0, 14, fill="#13a89e", outline="")
        row = tk.Frame(body, bg="#ffffff")
        row.pack(fill="x", pady=(8, 0))
        self.update_progress_detail = tk.Label(row, text="Connecting to GitHub…", bg="#ffffff",
                                               fg="#697b8c", font=("Segoe UI", 9))
        self.update_progress_detail.pack(side="left")
        self.update_progress_text = tk.Label(row, text="0%", bg="#ffffff", fg="#14304a",
                                             font=("Segoe UI", 9, "bold"))
        self.update_progress_text.pack(side="right")
        tk.Label(body, text="SNOW Reminder will reopen after installation.", bg="#ffffff", fg="#8a99a8",
                 font=("Segoe UI", 8)).pack(anchor="w", pady=(12, 0))
        window.grab_set()
        threading.Thread(target=self._download_update, args=(info,), daemon=True,
                         name="SNOW update download").start()

    def _set_update_progress(self, downloaded: int, total: int, status: str = "Downloading update") -> None:
        if self.update_window is None or not self.update_window.winfo_exists():
            return
        fraction = max(0.0, min(1.0, downloaded / total)) if total > 0 else 0.0
        if self.update_progress_canvas is not None and self.update_progress_canvas.winfo_exists():
            canvas = self.update_progress_canvas
            width = max(1, canvas.winfo_width())
            canvas.coords(self.update_progress_fill, 0, 0, int(width * fraction), 14)
        if self.update_progress_text is not None and self.update_progress_text.winfo_exists():
            self.update_progress_text.configure(text=f"{int(fraction * 100)}%")
        if self.update_progress_detail is not None and self.update_progress_detail.winfo_exists():
            if total > 0:
                detail = f"{downloaded / (1024 * 1024):.1f} MB of {total / (1024 * 1024):.1f} MB  ·  {status}"
            else:
                detail = status
            self.update_progress_detail.configure(text=detail)

    def _download_update(self, info: dict) -> None:
        file_descriptor, target_name = tempfile.mkstemp(prefix="SNOW-Reminder-Setup-", suffix=".exe")
        os.close(file_descriptor)
        target = Path(target_name)
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
                    total = int(info.get("size", 0))
                    self.root.after(0, lambda done=downloaded, size=total:
                                    self._set_update_progress(done, size))
            self.root.after(0, lambda: self._set_update_progress(
                downloaded, int(info.get("size", downloaded)), "Verifying checksum"))
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
        self.update_progress_canvas = None
        self.update_progress_fill = None
        self.update_progress_text = None
        self.update_progress_detail = None

    def _launch_update(self, installer: Path) -> None:
        self._close_update_window()
        try:
            subprocess.Popen([
                str(installer), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/SP-",
            ])
        except OSError as exc:
            self.update_prompt_active = False
            messagebox.showerror(APP_NAME, f"Could not launch the installer.\n\n{exc}")
            return
        self.stop_sound()
        self.shutting_down = True
        self.icon.stop()
        self.root.destroy()

    def schedule_next(self) -> None:
        if self.next_alert_id is not None:
            self.root.after_cancel(self.next_alert_id)
        delay = max(1, int(self.settings["interval_minutes"])) * 60 * 1000
        self.next_alert_at = time.monotonic() + delay / 1000
        self.next_alert_id = self.root.after(delay, self.raise_alert)
        self._refresh_tray_icon()

    def raise_alert(self) -> None:
        self.next_alert_id = None
        self.next_alert_at = None
        self._refresh_tray_icon()
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
        self.shutting_down = True
        self.icon.stop()
        self.root.destroy()

    def run(self) -> None:
        self._start_tray_icon()
        self.schedule_next()
        self.root.after(0, self._tick_tray_icon)
        if not self.settings.get("snow_url", "").strip():
            self.root.after(150, self.show_settings)
        self.root.after(15_000, self.check_for_updates)
        self.root.after(UPDATE_CHECK_MS, self._schedule_update_check)
        self.root.mainloop()

    def _schedule_update_check(self) -> None:
        self.check_for_updates()
        self.root.after(UPDATE_CHECK_MS, self._schedule_update_check)


if __name__ == "__main__":
    SnowReminder().run()
