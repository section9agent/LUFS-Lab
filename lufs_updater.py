"""LUFS Lab - saved settings and self-update from GitHub Releases.

No window code lives here, so it also works from the command line
(--version, --check-update, --update).

How an update is trusted: the download address must be on this project's own GitHub
releases page, and the file must match the SHA-256 checksum published next to it
(SHA256SUMS.txt) in the same release. A release without that file is never installed
automatically.
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile

APP_VERSION = "1.0.0"                       # stamped from the release tag by the build workflow
UPDATE_REPO = "section9agent/lufs-lab"      # GitHub "owner/name" that publishes the releases
CHECK_EVERY_SECONDS = 12 * 3600             # automatic checks at most twice a day
SUMS_NAME = "SHA256SUMS.txt"

DEFAULT_SETTINGS = {"auto_update": True, "skipped_version": "", "last_check": 0.0}


class UpdateError(Exception):
    """An update problem whose message is fit to show to the user."""


# --------------------------------------------------------------------------- #
#  Settings
# --------------------------------------------------------------------------- #
def config_dir() -> str:
    override = os.environ.get("LUFS_LAB_CONFIG_DIR")
    if override:
        return override
    if sys.platform.startswith("win"):
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
    elif sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Application Support")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "LUFS Lab")


def settings_path() -> str:
    return os.path.join(config_dir(), "settings.json")


def load_settings() -> dict:
    s = dict(DEFAULT_SETTINGS)
    try:
        with open(settings_path(), "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            for key, default in DEFAULT_SETTINGS.items():
                val = data.get(key, default)
                if isinstance(default, bool):
                    ok = isinstance(val, bool)
                elif isinstance(default, float):
                    ok = isinstance(val, (int, float)) and not isinstance(val, bool)
                else:
                    ok = isinstance(val, type(default))
                if ok:
                    s[key] = val
    except (OSError, ValueError):
        pass
    return s


def save_settings(settings: dict) -> bool:
    try:
        os.makedirs(config_dir(), exist_ok=True)
        tmp = settings_path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({k: settings.get(k, v) for k, v in DEFAULT_SETTINGS.items()}, fh, indent=2)
        os.replace(tmp, settings_path())
        return True
    except OSError:
        return False


def auto_check_due(settings: dict, now: float | None = None) -> bool:
    if not settings.get("auto_update", True):
        return False
    now = time.time() if now is None else now
    return now - float(settings.get("last_check", 0.0)) >= CHECK_EVERY_SECONDS


# --------------------------------------------------------------------------- #
#  Versions
# --------------------------------------------------------------------------- #
def parse_version(text: str) -> tuple:
    m = re.match(r"\s*[vV]?(\d+(?:\.\d+)*)", text or "")
    return tuple(int(p) for p in m.group(1).split(".")) if m else ()


def is_newer(candidate: str, current: str | None = None) -> bool:
    a, b = parse_version(candidate), parse_version(current or APP_VERSION)
    if not a:
        return False
    n = max(len(a), len(b))
    a, b = a + (0,) * (n - len(a)), b + (0,) * (n - len(b))
    return a > b


# --------------------------------------------------------------------------- #
#  Talking to GitHub
# --------------------------------------------------------------------------- #
def _api_base() -> str:
    override = os.environ.get("LUFS_LAB_API", "")          # only for local testing
    if override.startswith(("http://127.0.0.1", "http://localhost")):
        return override.rstrip("/")
    return "https://api.github.com"


def _allowed_download(url: str) -> bool:
    base = _api_base()
    if base != "https://api.github.com":
        return url.startswith(base + "/")
    return url.lower().startswith(f"https://github.com/{UPDATE_REPO}/releases/download/".lower())


def _open(url: str, timeout: float):
    req = urllib.request.Request(url, headers={
        "User-Agent": f"LUFS-Lab/{APP_VERSION}",
        "Accept": "application/vnd.github+json, application/octet-stream",
    })
    return urllib.request.urlopen(req, timeout=timeout)


def _fetch(url: str, timeout: float = 15, limit: int = 5_000_000) -> bytes:
    try:
        with _open(url, timeout) as r:
            return r.read(limit)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise UpdateError("No published release was found yet.")
        if exc.code in (403, 429):
            raise UpdateError("GitHub is limiting requests right now. Please try again later.")
        raise UpdateError(f"GitHub answered with an error ({exc.code}).")
    except (urllib.error.URLError, OSError, TimeoutError):
        raise UpdateError("Could not reach GitHub. Check your internet connection.")


def check_latest(timeout: float = 15) -> dict:
    """Ask GitHub for the newest published release. Raises UpdateError with a friendly message."""
    raw = _fetch(f"{_api_base()}/repos/{UPDATE_REPO}/releases/latest", timeout)
    try:
        data = json.loads(raw.decode("utf-8"))
        tag = str(data["tag_name"])
    except (ValueError, KeyError, TypeError):
        raise UpdateError("GitHub sent an answer that could not be understood.")
    assets = {a["name"]: a["browser_download_url"] for a in data.get("assets", [])
              if isinstance(a, dict) and "name" in a and "browser_download_url" in a}
    version = tag.lstrip("vV")
    return {
        "version": version, "tag": tag, "notes": data.get("body") or "",
        "page": data.get("html_url") or f"https://github.com/{UPDATE_REPO}/releases",
        "assets": assets, "newer": is_newer(version),
    }


# --------------------------------------------------------------------------- #
#  What kind of install is this?
# --------------------------------------------------------------------------- #
def _mac_bundle() -> str | None:
    p = os.path.abspath(sys.executable)
    while p and p != os.path.dirname(p):
        if p.endswith(".app"):
            return p
        p = os.path.dirname(p)
    return None


def install_kind() -> str:
    """exe | appimage | mac_app | source | manual (a stand-alone build that cannot replace itself)."""
    if getattr(sys, "frozen", False):
        if sys.platform.startswith("win"):
            return "exe"
        if sys.platform == "darwin":
            return "mac_app" if _mac_bundle() else "manual"
        return "appimage" if os.environ.get("APPIMAGE") else "manual"
    return "source"


def asset_name(kind: str) -> str | None:
    m = platform.machine()
    return {"exe": "LUFS-Lab.exe", "appimage": f"LUFS-Lab-{m}.AppImage",
            "mac_app": f"LUFS-Lab-mac-{m}.zip", "source": "LUFS-Lab-source.zip"}.get(kind)


# --------------------------------------------------------------------------- #
#  Download, verify, install
# --------------------------------------------------------------------------- #
def parse_sums(text: str) -> dict:
    out = {}
    for line in text.splitlines():
        m = re.match(r"^([0-9a-fA-F]{64})\s+\*?(.+?)\s*$", line)
        if m:
            out[m.group(2)] = m.group(1).lower()
    return out


def _download(url, dest, progress, cancelled) -> str:
    h = hashlib.sha256()
    try:
        with _open(url, 30) as r, open(dest, "wb") as out:
            total = int(r.headers.get("Content-Length") or 0)
            got = 0
            while True:
                if cancelled():
                    raise UpdateError("The update was cancelled.")
                chunk = r.read(1 << 16)
                if not chunk:
                    break
                out.write(chunk)
                h.update(chunk)
                got += len(chunk)
                if total:
                    progress(min(1.0, got / total))
    except UpdateError:
        raise
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise UpdateError(f"The download failed ({exc}). Please try again.")
    return h.hexdigest()


def _safe_extract(zip_path: str, dest: str) -> None:
    with zipfile.ZipFile(zip_path) as z:
        root = os.path.realpath(dest)
        for info in z.infolist():
            target = os.path.realpath(os.path.join(dest, info.filename))
            if target != root and not target.startswith(root + os.sep):
                raise UpdateError("The download contained an unsafe file path and was rejected.")
        z.extractall(dest)
        for info in z.infolist():                           # restore executable bits
            mode = (info.external_attr >> 16) & 0o777
            if mode and not info.is_dir():
                os.chmod(os.path.join(dest, info.filename), mode)


def _need_writable(folder: str, what: str) -> None:
    if not os.access(folder, os.W_OK):
        raise UpdateError(f"LUFS Lab has no permission to change {what}. Move it to a folder you own, "
                          "or download the new version by hand from the releases page.")


def _install_source(zip_path, app_dir, tmp, pip) -> list:
    ex = os.path.join(tmp, "src")
    _safe_extract(zip_path, ex)
    root = next((d for d, _, files in os.walk(ex) if "lufs_lab.py" in files), None)
    if root is None:
        raise UpdateError("The downloaded update does not look like LUFS Lab and was not installed.")
    _need_writable(app_dir, "the program's folder")
    req_path = os.path.join(app_dir, "requirements.txt")
    try:
        old_req = open(req_path, encoding="utf-8").read()
    except OSError:
        old_req = ""
    backup = os.path.join(tmp, "backup")
    os.makedirs(backup)
    done = []
    try:
        for name in sorted(f for f in os.listdir(root) if os.path.isfile(os.path.join(root, f))):
            dst = os.path.join(app_dir, name)
            if os.path.exists(dst):
                shutil.copy2(dst, os.path.join(backup, name))
            staged = dst + ".new"
            shutil.copy2(os.path.join(root, name), staged)
            os.replace(staged, dst)
            done.append(name)
    except OSError as exc:
        for name in done:                                    # put everything back
            try:
                if os.path.exists(os.path.join(backup, name)):
                    shutil.copy2(os.path.join(backup, name), os.path.join(app_dir, name))
                else:
                    os.remove(os.path.join(app_dir, name))
            except OSError:
                pass
        raise UpdateError(f"Could not write the update ({exc}); the old version was restored.")
    try:
        new_req = open(req_path, encoding="utf-8").read()
    except OSError:
        new_req = old_req
    if pip and new_req != old_req:
        r = subprocess.run([sys.executable, "-m", "pip", "install", "--quiet", "-r", req_path],
                           capture_output=True, text=True, timeout=900)
        if r.returncode != 0:
            raise UpdateError("The update was installed, but its extra packages could not be downloaded. "
                              "Run the launcher once with an internet connection.")
    return [sys.executable, os.path.join(app_dir, "lufs_lab.py")]


def _install_appimage(new_path, target) -> list:
    _need_writable(os.path.dirname(target) or ".", target)
    staged = target + ".new"
    shutil.copy2(new_path, staged)
    os.chmod(staged, 0o755)
    os.replace(staged, target)                               # swapping the file is fine while it runs
    return [target]


def _install_exe(new_path, target) -> list:
    _need_writable(os.path.dirname(target) or ".", target)
    staged, old = target + ".new", target + ".old"
    shutil.copy2(new_path, staged)
    try:
        if os.path.exists(old):
            os.remove(old)
    except OSError:
        old = f"{target}.{int(time.time())}.old"
    os.replace(target, old)                                  # Windows allows renaming a running program
    try:
        os.replace(staged, target)
    except OSError as exc:
        os.replace(old, target)
        raise UpdateError(f"Could not put the new version in place ({exc}).")
    return [target]


def _install_mac_app(zip_path, bundle, tmp) -> list:
    ex = os.path.join(tmp, "ex")
    os.makedirs(ex)
    ditto = shutil.which("ditto")
    if ditto:
        subprocess.run([ditto, "-x", "-k", zip_path, ex], check=True)
    else:
        _safe_extract(zip_path, ex)
    new_app = next((os.path.join(ex, n) for n in os.listdir(ex) if n.endswith(".app")), None)
    if not new_app:
        raise UpdateError("The downloaded update does not contain the app and was not installed.")
    _need_writable(os.path.dirname(bundle), "the application")
    old = bundle + ".old"
    shutil.rmtree(old, ignore_errors=True)
    os.rename(bundle, old)
    try:
        if ditto:
            subprocess.run([ditto, new_app, bundle], check=True)
        else:
            shutil.copytree(new_app, bundle, symlinks=True)
    except Exception as exc:
        shutil.rmtree(bundle, ignore_errors=True)
        os.rename(old, bundle)
        raise UpdateError(f"Could not put the new version in place ({exc}).")
    shutil.rmtree(old, ignore_errors=True)
    try:
        subprocess.run(["xattr", "-dr", "com.apple.quarantine", bundle], capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        pass
    return ["open", "-n", bundle]


def apply_update(info: dict, progress=lambda f: None, cancelled=lambda: False,
                 kind: str | None = None, app_dir: str | None = None, pip: bool = True) -> list:
    """Download, verify and install the release in `info`. Returns the command that restarts the program."""
    kind = kind or install_kind()
    if kind == "manual":
        raise UpdateError("This copy of LUFS Lab cannot update itself. "
                          "Please download the new version from the releases page.")
    name = asset_name(kind)
    url = info["assets"].get(name)
    if not url:
        raise UpdateError(f"This release does not include a download for this computer ({name}).")
    sums_url = info["assets"].get(SUMS_NAME)
    if not sums_url:
        raise UpdateError(f"This release has no checksum file ({SUMS_NAME}), so it will not be installed automatically.")
    if not (_allowed_download(url) and _allowed_download(sums_url)):
        raise UpdateError("The download address is not on the project's GitHub page, so it was refused.")
    expected = parse_sums(_fetch(sums_url).decode("utf-8", "replace")).get(name)
    if not expected:
        raise UpdateError(f"The checksum file does not list {name}, so it will not be installed.")
    tmp = tempfile.mkdtemp(prefix="lufslab-update-")
    try:
        path = os.path.join(tmp, name)
        if _download(url, path, progress, cancelled) != expected:
            raise UpdateError("The downloaded file did not match its checksum, so it was NOT installed. Please try again.")
        if kind == "source":
            return _install_source(path, app_dir or os.path.dirname(os.path.abspath(__file__)), tmp, pip)
        if kind == "appimage":
            return _install_appimage(path, os.environ["APPIMAGE"])
        if kind == "exe":
            return _install_exe(path, os.path.abspath(sys.executable))
        return _install_mac_app(path, _mac_bundle(), tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def restart_app(argv: list) -> None:
    """Start the new version as a separate process (the caller then closes this one)."""
    env = dict(os.environ)
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"               # a stand-alone build must unpack afresh
    kwargs = {}
    if sys.platform.startswith("win"):
        kwargs["creationflags"] = 0x00000008 | 0x00000200    # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen(argv, env=env, close_fds=True, **kwargs)


def cleanup_old_files() -> None:
    """Remove leftovers of a previous update (best effort, never raises)."""
    try:
        if getattr(sys, "frozen", False):
            exe = os.path.abspath(sys.executable)
            folder = os.path.dirname(exe)
            if sys.platform.startswith("win"):
                for n in os.listdir(folder):
                    if n.startswith(os.path.basename(exe)) and n.endswith(".old"):
                        os.remove(os.path.join(folder, n))
            bundle = _mac_bundle() if sys.platform == "darwin" else None
            if bundle:
                shutil.rmtree(bundle + ".old", ignore_errors=True)
    except OSError:
        pass
