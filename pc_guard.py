#!/usr/bin/env python3
"""Shared busy/idle checks for pc-guard.

Used by the status HTTP service and the mid-job watcher. Read-only except
for reading proc/sys, GPU tools, the idle helper state file, and the
configured local model API on localhost.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_CONF_KEYS = {
    "idle_minutes",
    "gpu_util_threshold",
    "gpu_vram_busy_mb",
    "model_idle_before_sleep_minutes",
    "watch_interval_seconds",
    "status_port",
    "bind",
    "ollama_host",
    "llamacpp_url",
    "model_backend",
    "llamacpp_unit",
    "llamacpp_idle_stop_minutes",
    "bot_wake_grace_minutes",
    "unload_hook",
    "wol_interface",
    "wake_mac",
    "wake_broadcast",
    "game_patterns_extra",
    "desktop_gpu_names_extra",
    "gpu_backend",
}

_ENV_MAP = {key: "PC_GUARD_" + key.upper() for key in _CONF_KEYS}


@dataclass
class Config:
    idle_minutes: float = 15
    gpu_util_threshold: int = 15
    gpu_vram_busy_mb: int = 512
    model_idle_before_sleep_minutes: float = 10
    watch_interval_seconds: int = 20
    status_port: int = 8743
    bind: str = "127.0.0.1,tailscale"
    ollama_host: str = "http://127.0.0.1:11434"
    llamacpp_url: str = "http://127.0.0.1:8080"
    model_backend: str = "ollama"  # ollama, llamacpp, both, none
    # Optional systemd --user unit that runs llama-server on demand (see
    # systemd/llama-server.service.in). Empty = pc-guard never starts/stops it.
    llamacpp_unit: str = ""
    # Stop the managed llama-server after this long with no request running.
    llamacpp_idle_stop_minutes: float = 5
    # After a bot wake, ignore "recent input" for this long if nobody has
    # touched the PC since the wake (resume resets the idle timer). 0 = off.
    bot_wake_grace_minutes: float = 20
    unload_hook: str = ""
    wol_interface: str = ""
    wake_mac: str = ""
    wake_broadcast: str = ""
    game_patterns_extra: str = ""
    desktop_gpu_names_extra: str = ""
    gpu_backend: str = "auto"  # auto, nvidia, amd, none

    @property
    def idle_busy_seconds(self) -> int:
        return int(float(self.idle_minutes) * 60)

    @property
    def model_idle_before_sleep(self) -> int:
        return int(float(self.model_idle_before_sleep_minutes) * 60)

    @property
    def bind_hosts(self) -> list[str]:
        return [p.strip() for p in self.bind.split(",") if p.strip()]


def _parse_conf_text(text: str) -> dict[str, str]:
    data: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().lower()
        if key not in _CONF_KEYS:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        data[key] = value
    return data


def _coerce(key: str, value: str, current):
    typ = type(current)
    if typ is int:
        return int(float(value))
    if typ is float:
        return float(value)
    return value


def config_paths() -> list[Path]:
    """Search order: PC_GUARD_CONFIG, install dir, XDG config."""
    paths: list[Path] = []
    env = os.environ.get("PC_GUARD_CONFIG", "").strip()
    if env:
        paths.append(Path(env).expanduser())
    paths.append(Path(__file__).resolve().parent / "pc-guard.conf")
    xdg = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))
    paths.append(xdg / "pc-guard" / "pc-guard.conf")
    return paths


def load_config(overrides: dict | None = None) -> Config:
    cfg = Config()
    seen: set[Path] = set()
    for path in config_paths():
        resolved = path
        try:
            resolved = path.resolve()
        except OSError:
            pass
        if resolved in seen:
            continue
        seen.add(resolved)
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        parsed = _parse_conf_text(text)
        for key, value in parsed.items():
            setattr(cfg, key, _coerce(key, value, getattr(cfg, key)))

    for key, env_name in _ENV_MAP.items():
        if env_name in os.environ and os.environ[env_name] != "":
            setattr(cfg, key, _coerce(key, os.environ[env_name], getattr(cfg, key)))

    if overrides:
        for key, value in overrides.items():
            if key in _CONF_KEYS:
                setattr(cfg, key, value)
    return cfg


_CONFIG: Config | None = None


def config() -> Config:
    global _CONFIG
    if _CONFIG is None:
        _CONFIG = load_config()
    return _CONFIG


def reset_config_cache() -> None:
    global _CONFIG
    _CONFIG = None


# ---------------------------------------------------------------------------
# Constants that are not typically tuned in the conf file
# ---------------------------------------------------------------------------

DEFAULT_DESKTOP_GPU_NAMES = {
    # KDE Plasma
    "kwin_wayland",
    "kwin_wayland_wrapper",
    "kwin_x11",
    "kwin",
    "plasmashell",
    "ksmserver",
    "kded6",
    "kded5",
    "kded",
    "sddm-greeter",
    "sddm-greeter-qt6",
    "plasmalogin",
    "plasmalogin-greeter",
    "xdg-desktop-portal-kde",
    "kscreenlocker",
    "kscreenlocker_greet",
    # Shared / generic
    "Xwayland",
    "xwayland",
    "xdg-desktop-portal",
    # GNOME
    "gnome-shell",
    "mutter",
    "gnome-session",
    "gnome-session-binary",
    "gnome-shell-calendar-server",
    "gsd-xsettings",
    "xdg-desktop-portal-gnome",
    "gdm-wayland-session",
    "gdm-x-session",
}

MODEL_NAME_RE = re.compile(
    r"(^|/)(ollama|ollama_llama_server|ollama[_-]runner|llama-server|llama-cli|"
    r"llama-bench|llama\.cpp)(\s|$)|ollama|llama-server",
    re.IGNORECASE,
)

OLLAMA_NAME_RE = MODEL_NAME_RE  # backward-compatible alias

STEAM_CLIENT_RE = re.compile(
    r"(steamwebhelper|steam-runtime-launcher|steam\.sh\b|/usr/bin/steam\b|"
    r"~/.local/share/Steam/ubuntu12|steam -psilib|steambridge)",
    re.IGNORECASE,
)

DEFAULT_GAME_PATTERNS = [
    r"\bgamescope\b",
    r"\bwineserver\b",
    r"\bwine64(\.exe)?\b",
    r"\bwine-preloader\b",
    r"\bwine\d*\.exe\b",
    r"\bproton(?:-|\d|\.|\s|$)",
    r"steamapps/common",
    r"pressure-vessel",
    r"SteamLinuxRuntime",
    r"\blutris-wrapper\b",
    r"\blutris\b",
    r"\bheroic\b",
    r"\bgamemoderun\b",
    r"\bumu-run\b",
    r"\blegendary\b",
    r"\bwine-staging\b",
]

STEAM_REAPER_RE = re.compile(r"\breaper\b", re.IGNORECASE)
STEAM_HINT_RE = re.compile(r"steam", re.IGNORECASE)


def desktop_gpu_names() -> set[str]:
    names = set(DEFAULT_DESKTOP_GPU_NAMES)
    extra = config().desktop_gpu_names_extra
    if extra:
        for part in re.split(r"[,\s]+", extra):
            if part:
                names.add(part)
    return names


def game_regexes() -> list[re.Pattern[str]]:
    patterns = list(DEFAULT_GAME_PATTERNS)
    extra = config().game_patterns_extra
    if extra:
        for part in extra.split(","):
            part = part.strip()
            if part:
                patterns.append(part)
    return [re.compile(p, re.IGNORECASE) for p in patterns]


# ---------------------------------------------------------------------------
# Runtime paths and helpers
# ---------------------------------------------------------------------------

def _runtime_dir() -> Path:
    base = Path(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}")
    path = base / "pc-guard"
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    return path


def woken_flag() -> Path:
    return _runtime_dir() / "woken-by-bot"


def simulate_busy_flag() -> Path:
    return _runtime_dir() / "simulate-busy"


def no_autosuspend_flag() -> Path:
    return _runtime_dir() / "no-autosuspend"


def last_model_seen_path() -> Path:
    return _runtime_dir() / "last-model-seen"


def install_dir() -> Path:
    return Path(__file__).resolve().parent


def run(cmd: list[str], timeout: float = 5) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired:
        return 1, "", "timeout"
    except FileNotFoundError as exc:
        return 127, "", str(exc)
    except OSError as exc:
        return 1, "", str(exc)


def tailscale_ipv4() -> str | None:
    """Resolve the current Tailscale IPv4 address, or None if unavailable."""
    code, out, _ = run(["tailscale", "ip", "-4"], timeout=3)
    if code == 0:
        ip = out.strip().splitlines()[0].strip() if out.strip() else ""
        if ip:
            return ip
    code, out, _ = run(["ip", "-4", "-o", "addr", "show", "dev", "tailscale0"], timeout=3)
    if code == 0:
        m = re.search(r"inet\s+(\d+\.\d+\.\d+\.\d+)", out)
        if m:
            return m.group(1)
    return None


def resolve_bind_hosts() -> list[str]:
    """Expand configured bind tokens such as 'tailscale' into IPs."""
    hosts: list[str] = []
    for token in config().bind_hosts:
        lower = token.lower()
        if lower in {"tailscale", "tailscale0"}:
            ip = tailscale_ipv4()
            if ip and ip not in hosts:
                hosts.append(ip)
            continue
        if token not in hosts:
            hosts.append(token)
    if not hosts:
        hosts = ["127.0.0.1"]
    return hosts


# ---------------------------------------------------------------------------
# Idle time
# ---------------------------------------------------------------------------

def _idle_state_path() -> Path:
    return _runtime_dir() / "idle.json"


def read_idle_helper() -> dict | None:
    """Read the KIdleTime helper's state file if it is fresh (under 5s old)."""
    path = _idle_state_path()
    try:
        raw = path.read_text()
        st = path.stat()
    except OSError:
        return None
    age = time.time() - st.st_mtime
    if age > 5:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    data["_age"] = age
    return data


def screens_dpms_off() -> tuple[bool | None, str]:
    """Return (all_off, detail) from `kscreen-doctor --dpms show`.

    Read-only: it never changes DPMS state. None means unknown or unavailable
    (for example on a non-KDE desktop).
    """
    code, out, err = run(["kscreen-doctor", "--dpms", "show"], timeout=8)
    text = (out or "") + (err or "")
    modes = re.findall(
        r"dpms mode for screen\s+(\S+)\s*:\s*(on|off|standby|suspend|unknown)",
        text,
        flags=re.IGNORECASE,
    )
    if not modes:
        return None, "kscreen-doctor DPMS unavailable"
    offs = [name for name, mode in modes if mode.lower() == "off"]
    ons = [name for name, mode in modes if mode.lower() != "off"]
    if ons:
        return False, "DPMS on: " + ", ".join(ons)
    return True, "DPMS off: " + ", ".join(offs)


def screensaver_active() -> bool | None:
    """org.freedesktop.ScreenSaver.GetActive. None if the call fails."""
    for tool in ("qdbus6", "qdbus", "gdbus"):
        if tool == "gdbus":
            cmd = [
                "gdbus",
                "call",
                "--session",
                "--dest",
                "org.freedesktop.ScreenSaver",
                "--object-path",
                "/ScreenSaver",
                "--method",
                "org.freedesktop.ScreenSaver.GetActive",
            ]
        else:
            cmd = [
                tool,
                "org.freedesktop.ScreenSaver",
                "/ScreenSaver",
                "GetActive",
            ]
        code, out, _ = run(cmd, timeout=3)
        if code != 0:
            continue
        val = out.strip().lower()
        # gdbus prints (true,) or (false,)
        if "true" in val or val in {"true", "1"}:
            return True
        if "false" in val or val in {"false", "0"}:
            return False
    return None


def _xprintidle_seconds() -> int | None:
    code, out, _ = run(["xprintidle"], timeout=3)
    if code != 0 or not out.strip():
        return None
    try:
        return max(0, int(out.strip()) // 1000)
    except ValueError:
        return None


def _mutter_idle_seconds() -> int | None:
    """GNOME / Mutter session idle time, in seconds."""
    code, out, _ = run(
        [
            "gdbus",
            "call",
            "--session",
            "--dest",
            "org.gnome.Mutter.IdleMonitor",
            "--object-path",
            "/org/gnome/Mutter/IdleMonitor/Core",
            "--method",
            "org.gnome.Mutter.IdleMonitor.GetIdletime",
        ],
        timeout=3,
    )
    if code != 0:
        return None
    m = re.search(r"\(?\s*(\d+)", out)
    if not m:
        return None
    try:
        return max(0, int(m.group(1)) // 1000)
    except ValueError:
        return None


def get_idle_seconds() -> tuple[int | None, str]:
    """Seconds since last keyboard/mouse input.

    Primary: a user-session helper (`idle_helper`) that uses KDE's KIdleTime
    on Wayland. KIdleTime::idleTime() itself often returns 0 on the KWin
    Wayland plugin, but the timeoutReached and resumingFromIdle signals work,
    and that is what the helper records.

    If the helper starts while the session is already idle, its count is
    only a lower bound. When every display is DPMS-off and the compositor
    also reports idle, that is treated as at least the configured input
    window, because typical desktop power settings only turn the screens
    off after the user has already been idle.

    logind IdleHint is not used as a busy signal: on some KDE Plasma Wayland
    sessions it stays "no" even with the screens off, which would falsely
    mark the PC as busy.

    If the helper is not running, X11 (`xprintidle`) and GNOME (Mutter
    IdleMonitor) fallbacks are tried. Unknown idle time is NOT treated as
    recent input.

    Returns (seconds, method).
    """
    helper = read_idle_helper()
    dpms_off, dpms_detail = screens_dpms_off()
    ss_active = screensaver_active()
    threshold = config().idle_busy_seconds

    if helper is not None:
        compositor_idle = bool(helper.get("compositor_idle"))
        try:
            idle_ms = int(helper.get("idle_ms") or 0)
        except (TypeError, ValueError):
            idle_ms = 0
        lower = bool(helper.get("idle_ms_is_lower_bound"))
        if not compositor_idle:
            return 0, "KIdleTime (recent input)"
        idle = max(0, idle_ms // 1000)
        if lower and dpms_off:
            idle = max(idle, threshold)
            return idle, "KIdleTime + DPMS off (lower bound; helper started mid-idle)"
        if lower and ss_active:
            idle = max(idle, threshold)
            return idle, "KIdleTime + screensaver (lower bound; helper started mid-idle)"
        method = "KIdleTime (lower bound)" if lower else "KIdleTime"
        return idle, method

    xp = _xprintidle_seconds()
    if xp is not None:
        return xp, "xprintidle"

    mutter = _mutter_idle_seconds()
    if mutter is not None:
        return mutter, "Mutter IdleMonitor"

    if dpms_off:
        return threshold, "kscreen-doctor " + dpms_detail + " (fallback)"
    if ss_active:
        return threshold, "ScreenSaver GetActive (fallback)"
    return None, "idle unknown (no helper, screens on or unknown)"


# ---------------------------------------------------------------------------
# GPU
# ---------------------------------------------------------------------------

def _basename(path_or_name: str) -> str:
    return Path(path_or_name.strip()).name


def _is_model_proc(name: str, base: str) -> bool:
    return bool(MODEL_NAME_RE.search(name) or MODEL_NAME_RE.search(base))


def gpu_snapshot_nvidia() -> dict:
    """GPU util, total VRAM used, and per-process VRAM from nvidia-smi."""
    result = {
        "gpu_util": None,
        "gpu_mem_used_mb": None,
        "processes": [],
        "error": None,
        "backend": "nvidia",
    }
    code, out, err = run(
        [
            "nvidia-smi",
            "--query-gpu=utilization.gpu,memory.used",
            "--format=csv,noheader,nounits",
        ],
        timeout=5,
    )
    if code != 0:
        result["error"] = (err or out or "nvidia-smi failed").strip()[:200]
        return result
    line = out.strip().splitlines()[0] if out.strip() else ""
    parts = [p.strip() for p in line.split(",")]
    if len(parts) >= 2:
        try:
            result["gpu_util"] = int(float(parts[0]))
        except ValueError:
            pass
        try:
            result["gpu_mem_used_mb"] = int(float(parts[1]))
        except ValueError:
            pass

    code2, out2, _ = run(["nvidia-smi"], timeout=6)
    procs = []
    if code2 == 0 and out2:
        proc_re = re.compile(
            r"\|\s+\d+\s+\S+\s+\S+\s+(\d+)\s+\S+\s+(\S.*?)\s+(\d+)\s*MiB\s*\|"
        )
        in_proc = False
        ignore = desktop_gpu_names()
        for ln in out2.splitlines():
            if "Processes:" in ln:
                in_proc = True
                continue
            if not in_proc:
                continue
            m = proc_re.search(ln)
            if not m:
                continue
            pid = int(m.group(1))
            name = m.group(2).strip()
            mem = int(m.group(3))
            base = _basename(name)
            procs.append(
                {
                    "pid": pid,
                    "name": name,
                    "base": base,
                    "mem_mb": mem,
                    "is_model": _is_model_proc(name, base),
                    "is_ollama": _is_model_proc(name, base),
                    "is_desktop": base in ignore,
                }
            )
    result["processes"] = procs
    return result


def gpu_snapshot_amd() -> dict:
    """Best-effort AMD util. Per-process VRAM is not always available."""
    result = {
        "gpu_util": None,
        "gpu_mem_used_mb": None,
        "processes": [],
        "error": None,
        "backend": "amd",
    }
    # sysfs: first card that exposes gpu_busy_percent
    utils = []
    try:
        drm = Path("/sys/class/drm")
        for card in sorted(drm.glob("card*/device/gpu_busy_percent")):
            try:
                utils.append(int(card.read_text().strip()))
            except (OSError, ValueError):
                continue
    except OSError:
        pass
    if utils:
        result["gpu_util"] = max(utils)
        return result

    code, out, err = run(["rocm-smi", "--showuse"], timeout=5)
    if code == 0:
        m = re.search(r"GPU use \(%\):\s*(\d+)", out or "", re.IGNORECASE)
        if not m:
            m = re.search(r"(\d+)\s*%", out or "")
        if m:
            result["gpu_util"] = int(m.group(1))
            return result

    result["error"] = (err or out or "AMD GPU util unavailable").strip()[:200]
    return result


def gpu_snapshot() -> dict:
    backend = (config().gpu_backend or "auto").lower()
    if backend == "none":
        return {
            "gpu_util": None,
            "gpu_mem_used_mb": None,
            "processes": [],
            "error": None,
            "backend": "none",
        }
    if backend == "nvidia":
        return gpu_snapshot_nvidia()
    if backend == "amd":
        return gpu_snapshot_amd()
    # auto: prefer nvidia-smi when present
    code, _, _ = run(["which", "nvidia-smi"], timeout=2)
    if code == 0:
        snap = gpu_snapshot_nvidia()
        if not snap.get("error"):
            return snap
    amd = gpu_snapshot_amd()
    if amd.get("gpu_util") is not None:
        return amd
    if code == 0:
        return gpu_snapshot_nvidia()
    return amd


def gpu_busy_reasons(gpu: dict, loaded_models: list[str]) -> list[str]:
    reasons = []
    if gpu.get("error"):
        return reasons
    threshold_mem = config().gpu_vram_busy_mb
    threshold_util = config().gpu_util_threshold
    heavy = []
    for p in gpu.get("processes") or []:
        if p.get("is_model") or p.get("is_ollama") or p.get("is_desktop"):
            continue
        if p.get("mem_mb", 0) >= threshold_mem:
            heavy.append(f"{p['base']} {p['mem_mb']}MB")
    if heavy:
        reasons.append("non-model VRAM: " + ", ".join(heavy))

    util = gpu.get("gpu_util")
    if util is not None and util > threshold_util:
        model_on_gpu = any(
            p.get("is_model") or p.get("is_ollama") for p in gpu.get("processes") or []
        )
        if model_on_gpu or loaded_models:
            # Util is attributed to the loaded model; not a "user is busy" signal.
            pass
        else:
            reasons.append(f"GPU util {util}% not from a local model")
    return reasons


# ---------------------------------------------------------------------------
# Games and models
# ---------------------------------------------------------------------------

def _proc_texts() -> list[tuple[str, str, str]]:
    """Yield (pid, comm, cmdline) for user-visible processes."""
    out = []
    try:
        entries = Path("/proc").iterdir()
    except OSError:
        return out
    for entry in entries:
        name = entry.name
        if not name.isdigit():
            continue
        try:
            comm = (entry / "comm").read_text(errors="replace").strip()
        except (FileNotFoundError, PermissionError, ProcessLookupError, OSError):
            continue
        try:
            raw = (entry / "cmdline").read_bytes()
            cmdline = raw.replace(b"\x00", b" ").decode("utf-8", "replace").strip()
        except (FileNotFoundError, PermissionError, ProcessLookupError, OSError):
            cmdline = ""
        out.append((name, comm, cmdline))
    return out


def game_processes() -> list[str]:
    found = []
    seen = set()
    game_res = game_regexes()
    for pid, comm, cmdline in _proc_texts():
        text = f"{comm} {cmdline}"
        if not text.strip():
            continue
        if STEAM_CLIENT_RE.search(text) and not any(
            r.search(text) for r in game_res
        ) and not (STEAM_REAPER_RE.search(text) and STEAM_HINT_RE.search(text)):
            continue
        matched = None
        for r in game_res:
            if r.search(text):
                matched = r.pattern
                break
        if matched is None and STEAM_REAPER_RE.search(comm) and STEAM_HINT_RE.search(text):
            matched = "steam-reaper"
        if not matched:
            continue
        label = comm or _basename(cmdline.split()[0] if cmdline else comm)
        if label in seen:
            continue
        seen.add(label)
        found.append(label)
    return found


def ollama_loaded_models() -> list[str]:
    host = config().ollama_host.rstrip("/")
    try:
        req = Request(host + "/api/ps", method="GET")
        with urlopen(req, timeout=3) as resp:
            body = resp.read().decode("utf-8", "replace")
        data = json.loads(body)
    except (URLError, TimeoutError, json.JSONDecodeError, OSError, ValueError):
        code, out, _ = run(["ollama", "ps"], timeout=5)
        if code != 0 or not out.strip():
            return []
        models = []
        lines = out.strip().splitlines()
        if lines and lines[0].lower().startswith("name"):
            lines = lines[1:]
        for ln in lines:
            parts = ln.split()
            if parts:
                models.append(parts[0])
        return models
    models = []
    for m in data.get("models") or []:
        name = m.get("name") or m.get("model")
        if name:
            models.append(name)
    return models


def llamacpp_loaded_models() -> list[str]:
    """Best-effort list of models served by llama-server, if it is up."""
    url = config().llamacpp_url.rstrip("/")
    for path in ("/v1/models", "/models", "/props"):
        try:
            req = Request(url + path, method="GET")
            with urlopen(req, timeout=2) as resp:
                body = resp.read().decode("utf-8", "replace")
            data = json.loads(body)
        except (URLError, TimeoutError, json.JSONDecodeError, OSError, ValueError):
            continue
        models: list[str] = []
        if isinstance(data, dict) and isinstance(data.get("data"), list):
            for m in data["data"]:
                if isinstance(m, dict) and m.get("id"):
                    models.append(str(m["id"]))
        elif isinstance(data, dict) and (data.get("model") or data.get("model_path")):
            models.append(str(data.get("model") or data.get("model_path")))
        if models:
            return models
        if isinstance(data, dict):
            return ["llama-server"]
    # Fall back to a running process name, if any.
    for _pid, comm, cmdline in _proc_texts():
        text = f"{comm} {cmdline}"
        if re.search(r"\bllama-server\b", text):
            return ["llama-server"]
    return []


def llamacpp_managed() -> bool:
    return bool((config().llamacpp_unit or "").strip())


def llama_unit_active() -> bool:
    """True if the managed llama-server unit is active or starting."""
    unit = (config().llamacpp_unit or "").strip()
    if not unit:
        return False
    _code, out, _ = run(["systemctl", "--user", "is-active", unit], timeout=5)
    return out.strip() in {"active", "activating", "reloading"}


def llama_health() -> bool:
    """True once llama-server answers GET /health with 200 (model loaded)."""
    url = config().llamacpp_url.rstrip("/")
    try:
        with urlopen(Request(url + "/health", method="GET"), timeout=3) as resp:
            resp.read()
            return getattr(resp, "status", 200) == 200
    except (URLError, TimeoutError, OSError, ValueError):
        return False


def llama_processing() -> bool | None:
    """True if any llama-server slot is working; None if unknown/loading."""
    url = config().llamacpp_url.rstrip("/")
    try:
        with urlopen(Request(url + "/slots", method="GET"), timeout=3) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except (URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(data, list):
        return None
    return any(bool(s.get("is_processing")) for s in data if isinstance(s, dict))


def start_llama() -> bool:
    unit = (config().llamacpp_unit or "").strip()
    if not unit:
        return False
    run(["systemctl", "--user", "reset-failed", unit], timeout=10)
    code, _, _ = run(["systemctl", "--user", "start", unit], timeout=30)
    return code == 0


def stop_llama() -> bool:
    unit = (config().llamacpp_unit or "").strip()
    if not unit:
        return False
    code, _, _ = run(["systemctl", "--user", "stop", unit], timeout=30)
    return code == 0


def llama_loaded_models() -> list[str]:
    """Models served by llama-server (managed unit, or any if unmanaged)."""
    if (config().model_backend or "ollama").lower() not in {"llamacpp", "both"}:
        return []
    if llamacpp_managed() and not llama_unit_active():
        return []
    models = llamacpp_loaded_models()
    if llamacpp_managed() and not models:
        return ["llama-server (loading)"]
    return models


def apply_bot_wake_grace(status: dict, now: float | None = None) -> dict:
    """Right after a bot wake, drop input-only busy reasons if nobody has
    touched the PC since the wake. Games and GPU load still count."""
    grace = float(config().bot_wake_grace_minutes) * 60
    if grace <= 0:
        return status
    try:
        since = (now if now is not None else time.time()) - woken_flag().stat().st_mtime
    except OSError:
        return status
    if since > grace:
        return status
    idle = status.get("idle_seconds")
    if not isinstance(idle, (int, float)) or idle + 60 < since:
        return status  # someone touched it after the wake
    old = list(status.get("reasons") or [])
    reasons = [r for r in old if not str(r).startswith("recent input")]
    if len(reasons) == len(old):
        return status
    out = dict(status)
    out["reasons"] = reasons
    out["busy"] = bool(reasons)
    return out


def loaded_models() -> list[str]:
    backend = (config().model_backend or "ollama").lower()
    models: list[str] = []
    if backend in {"ollama", "both"}:
        models.extend(ollama_loaded_models())
    if backend in {"llamacpp", "both"}:
        models.extend(llama_loaded_models())
    # de-dupe, keep order
    seen = set()
    out = []
    for m in models:
        if m not in seen:
            seen.add(m)
            out.append(m)
    return out


def collect_status() -> dict:
    models = loaded_models()
    llama_models = llama_loaded_models()
    gpu = gpu_snapshot()
    idle, idle_method = get_idle_seconds()
    games = game_processes()
    reasons: list[str] = []

    if simulate_busy_flag().exists():
        reasons.append("simulate-busy flag")

    if idle is None:
        idle_out = None
    else:
        idle_out = int(idle)
        if idle_out < config().idle_busy_seconds:
            if idle_out == 0:
                reasons.append("recent input (session not idle)")
            else:
                reasons.append(f"recent input (idle {idle_out}s)")

    if games:
        reasons.append("game running: " + ", ".join(games))

    reasons.extend(gpu_busy_reasons(gpu, models))

    checked = datetime.now().astimezone().isoformat(timespec="seconds")
    return {
        "busy": bool(reasons),
        "reasons": reasons,
        "gpu_util": gpu.get("gpu_util"),
        "gpu_mem_used_mb": gpu.get("gpu_mem_used_mb"),
        "idle_seconds": idle_out if idle is not None else None,
        "game_running": bool(games),
        "loaded_models": models,
        "ollama_loaded_models": models,  # compatibility with the original field
        "llama_loaded_models": llama_models,
        "checked_at": checked,
        "idle_method": idle_method,
    }


def status_json(status: dict | None = None) -> str:
    data = status if status is not None else collect_status()
    public = {
        "busy": data["busy"],
        "reasons": data["reasons"],
        "gpu_util": data["gpu_util"],
        "gpu_mem_used_mb": data["gpu_mem_used_mb"],
        "idle_seconds": data["idle_seconds"],
        "game_running": data["game_running"],
        "loaded_models": data.get("loaded_models") or data.get("ollama_loaded_models") or [],
        "ollama_loaded_models": data.get("ollama_loaded_models")
        or data.get("loaded_models")
        or [],
        "llama_loaded_models": data.get("llama_loaded_models") or [],
        "checked_at": data["checked_at"],
    }
    return json.dumps(public, indent=2) + "\n"


if __name__ == "__main__":
    print(status_json(), end="")
