#!/usr/bin/env python3
"""Unit tests that do not need a GPU, a compositor, or a running model."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

# Load the module from the repo root.
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pc_guard  # noqa: E402


class ConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        pc_guard.reset_config_cache()
        self._env_backup = {
            key: os.environ[key] for key in list(os.environ) if key.startswith("PC_GUARD_")
        }
        for key in list(os.environ):
            if key.startswith("PC_GUARD_"):
                del os.environ[key]

    def tearDown(self) -> None:
        for key in list(os.environ):
            if key.startswith("PC_GUARD_"):
                del os.environ[key]
        os.environ.update(self._env_backup)
        pc_guard.reset_config_cache()

    def test_defaults(self) -> None:
        cfg = pc_guard.load_config()
        self.assertEqual(cfg.idle_busy_seconds, 15 * 60)
        self.assertEqual(cfg.status_port, 8743)
        self.assertEqual(cfg.bind_hosts, ["127.0.0.1", "tailscale"])

    def test_file_and_env_override(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "pc-guard.conf"
            path.write_text("idle_minutes=5\nstatus_port=9000\n# comment\nbind=127.0.0.1\n")
            os.environ["PC_GUARD_CONFIG"] = str(path)
            os.environ["PC_GUARD_STATUS_PORT"] = "9100"
            cfg = pc_guard.load_config()
            self.assertEqual(cfg.idle_busy_seconds, 5 * 60)
            self.assertEqual(cfg.status_port, 9100)
            self.assertEqual(cfg.bind_hosts, ["127.0.0.1"])

    def test_parse_ignores_unknown_and_quotes(self) -> None:
        parsed = pc_guard._parse_conf_text('idle_minutes="8"\nnot_a_key=1\n')
        self.assertEqual(parsed["idle_minutes"], "8")
        self.assertNotIn("not_a_key", parsed)


class GameDetectionTests(unittest.TestCase):
    def test_steam_client_is_not_a_game(self) -> None:
        # Monkeypatch process list: Steam client only.
        fake = [
            ("1", "steam", "/usr/bin/steam -psilib"),
            ("2", "steamwebhelper", "steamwebhelper --something"),
        ]
        with mock.patch.object(pc_guard, "_proc_texts", return_value=fake):
            self.assertEqual(pc_guard.game_processes(), [])

    def test_steamapps_common_is_a_game(self) -> None:
        fake = [
            ("10", "reaper", "reaper SteamLaunch steamapps/common/SomeGame/game"),
        ]
        with mock.patch.object(pc_guard, "_proc_texts", return_value=fake):
            games = pc_guard.game_processes()
        self.assertIn("reaper", games)

    def test_gamescope_is_a_game(self) -> None:
        fake = [("11", "gamescope", "gamescope -- steam")]
        with mock.patch.object(pc_guard, "_proc_texts", return_value=fake):
            self.assertIn("gamescope", pc_guard.game_processes())


class GpuReasonTests(unittest.TestCase):
    def test_desktop_and_model_vram_ignored(self) -> None:
        gpu = {
            "gpu_util": 40,
            "processes": [
                {"base": "kwin_wayland", "mem_mb": 800, "is_desktop": True, "is_model": False},
                {"base": "ollama", "mem_mb": 4000, "is_desktop": False, "is_model": True},
            ],
            "error": None,
        }
        # Model is loaded, so high util is not a busy reason.
        self.assertEqual(pc_guard.gpu_busy_reasons(gpu, ["qwen3:8b"]), [])

    def test_other_process_vram_is_busy(self) -> None:
        gpu = {
            "gpu_util": 5,
            "processes": [
                {"base": "blender", "mem_mb": 2048, "is_desktop": False, "is_model": False},
            ],
            "error": None,
        }
        reasons = pc_guard.gpu_busy_reasons(gpu, [])
        self.assertTrue(any("blender" in r for r in reasons))

    def test_util_without_model_is_busy(self) -> None:
        gpu = {"gpu_util": 80, "processes": [], "error": None}
        reasons = pc_guard.gpu_busy_reasons(gpu, [])
        self.assertTrue(any("GPU util" in r for r in reasons))


class StatusJsonTests(unittest.TestCase):
    def test_public_fields(self) -> None:
        status = {
            "busy": False,
            "reasons": [],
            "gpu_util": 1,
            "gpu_mem_used_mb": 200,
            "idle_seconds": 900,
            "game_running": False,
            "loaded_models": [],
            "ollama_loaded_models": [],
            "checked_at": "now",
            "idle_method": "test",
        }
        data = __import__("json").loads(pc_guard.status_json(status))
        self.assertNotIn("idle_method", data)
        self.assertIn("loaded_models", data)
        self.assertIn("ollama_loaded_models", data)
        self.assertFalse(data["busy"])


class BindHostTests(unittest.TestCase):
    def test_tailscale_token_dropped_when_missing(self) -> None:
        pc_guard.reset_config_cache()
        with mock.patch.object(pc_guard, "config", return_value=pc_guard.Config(bind="127.0.0.1,tailscale")):
            with mock.patch.object(pc_guard, "tailscale_ipv4", return_value=None):
                hosts = pc_guard.resolve_bind_hosts()
        self.assertEqual(hosts, ["127.0.0.1"])

    def test_tailscale_ip_included_when_present(self) -> None:
        # 100.64.0.2 is an RFC 6598 shared-address example, not a real host.
        example_ts_ip = "100.64.0.2"
        pc_guard.reset_config_cache()
        with mock.patch.object(pc_guard, "config", return_value=pc_guard.Config(bind="127.0.0.1,tailscale")):
            with mock.patch.object(pc_guard, "tailscale_ipv4", return_value=example_ts_ip):
                hosts = pc_guard.resolve_bind_hosts()
        self.assertEqual(hosts, ["127.0.0.1", example_ts_ip])


class BotWakeGraceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._env = mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": self._tmp.name})
        self._env.start()
        self._cfg = mock.patch.object(pc_guard, "config", return_value=pc_guard.Config())
        self._cfg.start()

    def tearDown(self) -> None:
        self._cfg.stop()
        self._env.stop()
        self._tmp.cleanup()

    def _status(self, reasons, idle):
        return {"busy": bool(reasons), "reasons": list(reasons), "idle_seconds": idle}

    def test_no_flag_keeps_input_reason(self) -> None:
        st = pc_guard.apply_bot_wake_grace(self._status(["recent input (idle 100s)"], 100))
        self.assertTrue(st["busy"])

    def test_fresh_wake_drops_input_reason(self) -> None:
        pc_guard.woken_flag().write_text("now\n")
        st = pc_guard.apply_bot_wake_grace(self._status(["recent input (idle 100s)"], 100))
        self.assertFalse(st["busy"])
        self.assertEqual(st["reasons"], [])

    def test_input_after_wake_still_busy(self) -> None:
        flag = pc_guard.woken_flag()
        flag.write_text("then\n")
        now = flag.stat().st_mtime + 400
        st = pc_guard.apply_bot_wake_grace(self._status(["recent input (idle 100s)"], 100), now=now)
        self.assertTrue(st["busy"])

    def test_game_still_busy(self) -> None:
        pc_guard.woken_flag().write_text("now\n")
        st = pc_guard.apply_bot_wake_grace(
            self._status(["recent input (idle 100s)", "game running: x"], 100)
        )
        self.assertTrue(st["busy"])
        self.assertEqual(st["reasons"], ["game running: x"])

    def test_grace_disabled(self) -> None:
        pc_guard.woken_flag().write_text("now\n")
        with mock.patch.object(pc_guard, "config", return_value=pc_guard.Config(bot_wake_grace_minutes=0)):
            st = pc_guard.apply_bot_wake_grace(self._status(["recent input (idle 100s)"], 100))
        self.assertTrue(st["busy"])


class LlamaManagedTests(unittest.TestCase):
    def test_unmanaged_by_default(self) -> None:
        with mock.patch.object(pc_guard, "config", return_value=pc_guard.Config()):
            self.assertFalse(pc_guard.llamacpp_managed())
            self.assertFalse(pc_guard.stop_llama())
            self.assertEqual(pc_guard.llama_loaded_models(), [])

    def test_managed_unit_inactive_reports_nothing(self) -> None:
        cfg = pc_guard.Config(model_backend="both", llamacpp_unit="llama-server.service")
        with mock.patch.object(pc_guard, "config", return_value=cfg), \
                mock.patch.object(pc_guard, "run", return_value=(3, "inactive\n", "")):
            self.assertEqual(pc_guard.llama_loaded_models(), [])

    def test_managed_unit_active_reports_models(self) -> None:
        cfg = pc_guard.Config(model_backend="both", llamacpp_unit="llama-server.service")
        with mock.patch.object(pc_guard, "config", return_value=cfg), \
                mock.patch.object(pc_guard, "run", return_value=(0, "active\n", "")), \
                mock.patch.object(pc_guard, "llamacpp_loaded_models", return_value=["local-model"]):
            self.assertEqual(pc_guard.llama_loaded_models(), ["local-model"])

    def test_managed_unit_loading(self) -> None:
        cfg = pc_guard.Config(model_backend="llamacpp", llamacpp_unit="llama-server.service")
        with mock.patch.object(pc_guard, "config", return_value=cfg), \
                mock.patch.object(pc_guard, "run", return_value=(0, "activating\n", "")), \
                mock.patch.object(pc_guard, "llamacpp_loaded_models", return_value=[]):
            self.assertEqual(pc_guard.llama_loaded_models(), ["llama-server (loading)"])

    def test_llama_vram_not_busy(self) -> None:
        gpu = {
            "gpu_util": 90,
            "error": None,
            "processes": [{"base": "llama-server", "name": "llama-server", "mem_mb": 6000,
                           "is_model": True, "is_ollama": True, "is_desktop": False}],
        }
        self.assertEqual(pc_guard.gpu_busy_reasons(gpu, ["local-model"]), [])

    def test_status_json_has_llama_field(self) -> None:
        status = {
            "busy": False, "reasons": [], "gpu_util": 0, "gpu_mem_used_mb": 0,
            "idle_seconds": 900, "game_running": False, "loaded_models": ["m"],
            "ollama_loaded_models": ["m"], "llama_loaded_models": ["m"], "checked_at": "now",
        }
        data = __import__("json").loads(pc_guard.status_json(status))
        self.assertEqual(data["llama_loaded_models"], ["m"])


if __name__ == "__main__":
    unittest.main()
