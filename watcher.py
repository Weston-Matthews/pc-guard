#!/usr/bin/env python3
"""Unload local models if the PC becomes busy, and sleep again after a bot wake.

- Every watch interval, read the same busy check as GET /status.
- If a model is loaded and the PC is busy from user activity, a game, or
  non-model GPU load (or the simulate-busy test flag), unload every loaded
  model. Never kills user apps. The default unload is `ollama stop`; set
  unload_hook or PC_GUARD_UNLOAD_HOOK for another backend such as llama.cpp.
- If a "woken by bot" flag is set, no model has been loaded for the
  configured idle period, and the PC is idle by the same rule, suspend.
  The flag is cleared first. Never suspends while someone is using the PC
  or a game is running.
- Does not change Ollama's default keep_alive and does not take a sleep
  inhibitor.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from datetime import datetime
from urllib.request import Request, urlopen

import pc_guard

LOG_DIR = pc_guard.install_dir() / "logs"
LOG_FILE = LOG_DIR / "watcher.log"


def setup_logging() -> logging.Logger:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("pc-guard-watcher")
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    fh = logging.FileHandler(LOG_FILE)
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.handlers.clear()
    logger.addHandler(fh)
    logger.addHandler(sh)
    logger.propagate = False
    return logger


log = setup_logging()


def _hook_path() -> str:
    return (pc_guard.config().unload_hook or os.environ.get("PC_GUARD_UNLOAD_HOOK") or "").strip()


def unload_models(models: list[str]) -> list[str]:
    """Unload each model. Never sends signals to arbitrary user processes.

    Order: a configured unload hook (if set), otherwise `ollama stop` with
    a keep_alive=0 API fallback. llama.cpp is never killed automatically;
    configure a hook if you want it unloaded.
    """
    hook = _hook_path()
    if hook:
        code, out, err = pc_guard.run([hook, *models], timeout=30)
        if code == 0:
            log.info("unloaded %s with hook %s", models, hook)
            return list(models)
        log.error("unload hook failed (code %s): %s %s", code, out, err)
        return []

    backend = (pc_guard.config().model_backend or "ollama").lower()
    if backend == "none":
        log.info("model_backend=none; not unloading %s", models)
        return []
    if backend == "llamacpp" and "ollama" not in backend:
        log.warning(
            "models %s are loaded but no unload_hook is set; "
            "refusing to kill llama-server automatically",
            models,
        )
        return []

    unloaded = []
    host = pc_guard.config().ollama_host.rstrip("/")
    for name in models:
        code, out, err = pc_guard.run(["ollama", "stop", name], timeout=30)
        if code == 0:
            log.info("unloaded %s with ollama stop", name)
            unloaded.append(name)
            continue
        log.warning("ollama stop %s failed (code %s): %s %s", name, code, out, err)
        try:
            body = json.dumps({"model": name, "keep_alive": 0, "prompt": ""}).encode()
            req = Request(
                host + "/api/generate",
                data=body,
                method="POST",
                headers={"Content-Type": "application/json"},
            )
            with urlopen(req, timeout=30) as resp:
                resp.read()
            log.info("unloaded %s with keep_alive=0", name)
            unloaded.append(name)
        except Exception as exc:  # noqa: BLE001
            log.error("failed to unload %s: %s", name, exc)
    return unloaded


def record_model_seen(when: float | None = None) -> None:
    path = pc_guard.last_model_seen_path()
    ts = when if when is not None else time.time()
    try:
        path.write_text(str(ts) + "\n")
    except OSError as exc:
        log.warning("could not write last-model-seen: %s", exc)


def last_model_seen_at() -> float:
    path = pc_guard.last_model_seen_path()
    try:
        return float(path.read_text().strip())
    except (OSError, ValueError):
        return time.time()


def user_or_game_busy(status: dict) -> bool:
    """Busy from input, a game, non-model GPU load, or the test flag.

    A loaded local model itself is not "user busy".
    """
    return bool(status.get("reasons"))


def try_suspend(log: logging.Logger) -> bool:
    """Suspend the machine without leaving an inhibitor.

    Clears the woken-by-bot flag first so a later manual wake does not
    immediately re-suspend. Uses systemctl --no-ask-password so we never
    hang on a password prompt.
    """
    flag = pc_guard.woken_flag()
    try:
        if flag.exists():
            flag.unlink()
            log.info("cleared woken-by-bot flag before suspend")
    except OSError as exc:
        log.error("could not clear woken-by-bot flag, aborting suspend: %s", exc)
        return False

    status = pc_guard.collect_status()
    models = status.get("loaded_models") or status.get("ollama_loaded_models")
    if user_or_game_busy(status) or models:
        log.info(
            "aborting suspend; re-check is busy or a model is loaded: %s",
            status.get("reasons"),
        )
        try:
            flag.write_text(datetime.now().astimezone().isoformat(timespec="seconds") + "\n")
        except OSError:
            pass
        return False

    code, out, err = pc_guard.run(
        ["systemctl", "--no-ask-password", "suspend"],
        timeout=15,
    )
    if code == 0:
        log.info("requested system suspend")
        return True
    log.warning("systemctl suspend failed (code %s): %s %s; trying loginctl", code, out, err)
    code2, out2, err2 = pc_guard.run(["loginctl", "suspend"], timeout=15)
    if code2 == 0:
        log.info("requested suspend via loginctl")
        return True
    log.error("suspend failed: systemctl=%s %s; loginctl=%s %s", out, err, out2, err2)
    try:
        flag.write_text(datetime.now().astimezone().isoformat(timespec="seconds") + "\n")
    except OSError:
        pass
    return False


def loop() -> None:
    interval = pc_guard.config().watch_interval_seconds
    log.info("pc-guard watcher starting (interval %ss)", interval)
    if not pc_guard.last_model_seen_path().exists():
        record_model_seen(time.time())

    while True:
        try:
            status = pc_guard.collect_status()
        except Exception as exc:  # noqa: BLE001
            log.error("status check failed: %s", exc)
            time.sleep(interval)
            continue

        models = status.get("loaded_models") or status.get("ollama_loaded_models") or []
        if models:
            record_model_seen()
            if user_or_game_busy(status):
                log.info(
                    "PC is busy while models %s are loaded (%s); unloading",
                    models,
                    status.get("reasons"),
                )
                unloaded = unload_models(models)
                record_model_seen()
                log.info("unload complete: %s", unloaded)

        else:
            flag = pc_guard.woken_flag()
            skip = pc_guard.no_autosuspend_flag()
            if flag.exists() and not skip.exists():
                idle = status.get("idle_seconds")
                idle_ok = (
                    idle is not None
                    and idle >= pc_guard.config().idle_busy_seconds
                    and not user_or_game_busy(status)
                )
                since_model = time.time() - last_model_seen_at()
                model_ok = since_model >= pc_guard.config().model_idle_before_sleep
                if idle_ok and model_ok:
                    log.info(
                        "woken-by-bot, idle %ss, no model for %.0fs; suspending",
                        idle,
                        since_model,
                    )
                    try_suspend(log)
        time.sleep(interval)


def main() -> int:
    try:
        loop()
    except KeyboardInterrupt:
        log.info("watcher stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
