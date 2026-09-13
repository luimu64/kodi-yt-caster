"""yt-dlp binary downloader and updater for Kodi."""

from __future__ import annotations

import logging
import os
import platform
import shutil
import sys
import tempfile
import time
import urllib.request
from typing import Optional, Tuple

logger = logging.getLogger("ytlounge.downloader")

try:
    import xbmc
    import xbmcaddon
    import xbmcgui
    import xbmcvfs
    KODI_AVAILABLE = True
except ImportError:
    KODI_AVAILABLE = False
    xbmc = None  # type: ignore
    xbmcaddon = None  # type: ignore
    xbmcgui = None  # type: ignore
    xbmcvfs = None  # type: ignore

GITHUB_RELEASES_URL = "https://github.com/yt-dlp/yt-dlp/releases/latest/download"


def get_platform_asset_name() -> Tuple[str, str]:
    """Return (asset_name_on_github, local_filename)."""
    sys_name = platform.system().lower()
    machine = platform.machine().lower()

    if "windows" in sys_name:
        return "yt-dlp.exe", "yt-dlp.exe"

    if "darwin" in sys_name:
        return "yt-dlp_macos", "yt-dlp"

    # Linux / Android / BSD
    if machine in ("aarch64", "arm64"):
        return "yt-dlp_linux_aarch64", "yt-dlp"
    elif machine in ("armv7l", "armv6l", "armhf"):
        return "yt-dlp_linux_armv7l", "yt-dlp"
    elif machine in ("x86_64", "amd64"):
        return "yt-dlp_linux", "yt-dlp"

    # Fallback universal python zipapp
    return "yt-dlp", "yt-dlp"


def get_binary_destination() -> str:
    """Return the absolute path where yt-dlp binary should be stored."""
    asset_name, filename = get_platform_asset_name()

    if KODI_AVAILABLE and xbmcaddon and xbmcvfs:
        try:
            profile = xbmcvfs.translatePath(xbmcaddon.Addon().getAddonInfo("profile"))
            bin_dir = os.path.join(profile, "bin")
            os.makedirs(bin_dir, exist_ok=True)
            return os.path.join(bin_dir, filename)
        except Exception as e:
            logger.warning("Could not resolve Kodi profile path: %s", e)

    # Fallback to resources/bin inside addon directory
    addon_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    bin_dir = os.path.join(addon_root, "resources", "bin")
    os.makedirs(bin_dir, exist_ok=True)
    return os.path.join(bin_dir, filename)


def download_ytdlp(force: bool = False, show_ui: bool = True) -> str:
    """Download or update yt-dlp binary."""
    dest_path = get_binary_destination()
    if os.path.isfile(dest_path) and not force:
        return dest_path

    asset_name, _ = get_platform_asset_name()
    download_url = f"{GITHUB_RELEASES_URL}/{asset_name}"

    logger.info("Downloading yt-dlp from %s to %s", download_url, dest_path)

    progress_dialog = None
    if show_ui and KODI_AVAILABLE and xbmcgui:
        try:
            progress_dialog = xbmcgui.DialogProgress()
            progress_dialog.create("YouTube Cast", f"Downloading yt-dlp ({asset_name})...")
        except Exception:
            progress_dialog = None

    headers = {
        "User-Agent": "Mozilla/5.0 (Kodi; YouTube Lounge Cast Receiver)",
        "Accept": "*/*",
    }
    req = urllib.request.Request(download_url, headers=headers)

    # Unique temp name so concurrent downloads (service start + settings
    # button) never corrupt each other.
    fd, tmp_path = tempfile.mkstemp(dir=os.path.dirname(dest_path) or ".", prefix=".ytdlp-", suffix=".tmp")
    os.close(fd)

    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            total_size = int(resp.headers.get("Content-Length", 0) or 0)
            downloaded = 0
            chunk_size = 64 * 1024

            with open(tmp_path, "wb") as out_file:
                while True:
                    if progress_dialog and progress_dialog.iscanceled():
                        raise RuntimeError("Download canceled by user")

                    chunk = resp.read(chunk_size)
                    if not chunk:
                        break
                    out_file.write(chunk)
                    downloaded += len(chunk)

                    if progress_dialog and total_size > 0:
                        pct = int((downloaded / total_size) * 100)
                        progress_dialog.update(
                            pct,
                            f"Downloading yt-dlp ({asset_name})...\n{downloaded // 1024} KB / {total_size // 1024} KB",
                        )

        # Sanity check: native binaries are >1MB; anything smaller is an error
        # page or truncated transfer.
        if downloaded < 1_000_000:
            raise RuntimeError(f"Downloaded yt-dlp looks truncated ({downloaded} bytes)")

        os.chmod(tmp_path, 0o755)
        # Atomic replace: never a window with no yt-dlp on disk.
        os.replace(tmp_path, dest_path)

        logger.info("Successfully installed yt-dlp to %s", dest_path)

        if show_ui and KODI_AVAILABLE and xbmcgui:
            xbmcgui.Dialog().notification("YouTube Cast", "yt-dlp updated successfully", xbmcgui.NOTIFICATION_INFO)

        return dest_path

    except Exception as e:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass
        logger.error("Failed to download yt-dlp: %s", e)
        if show_ui and KODI_AVAILABLE and xbmcgui:
            xbmcgui.Dialog().notification("YouTube Cast", f"Failed to download yt-dlp: {e}", xbmcgui.NOTIFICATION_ERROR)
        raise
    finally:
        if progress_dialog:
            try:
                progress_dialog.close()
            except Exception:
                pass


def ensure_ytdlp() -> Optional[str]:
    """Ensure yt-dlp is available, auto-downloading if missing."""
    dest = get_binary_destination()
    # Gate on file presence, not the exec bit: on noexec userdata mounts
    # os.access(X_OK) is always False and we would re-download on every boot.
    if os.path.isfile(dest):
        return dest

    # Check if existing system binary exists
    for name in ("yt-dlp", "yt-dlp.exe"):
        sys_path = shutil.which(name)
        if sys_path:
            return sys_path

    # Otherwise download it automatically on install / first run.
    # Back off for an hour after a failure so a broken network does not
    # re-download ~25MB on every service start.
    marker = dest + ".failed_at"
    try:
        if os.path.exists(marker) and time.time() - os.path.getmtime(marker) < 3600:
            logger.debug("Skipping yt-dlp download; recent attempt failed")
            return None
    except Exception:
        pass
    try:
        result = download_ytdlp(force=False, show_ui=True)
        if os.path.exists(marker):
            try:
                os.remove(marker)
            except Exception:
                pass
        return result
    except Exception as e:
        logger.warning("Automatic yt-dlp download failed: %s", e)
        try:
            with open(marker, "w") as f:
                f.write(str(time.time()))
        except Exception:
            pass
        return None


if __name__ == "__main__":
    download_ytdlp(force=True, show_ui=True)
