"""Unit tests for node-selection and health logic (no network)."""

from __future__ import annotations

import time
import unittest
from unittest.mock import patch

from clashpilot import config, daemon, health, selector


class OpusFilteringDefaultsTest(unittest.TestCase):
    def test_filtering_on_by_default(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            with patch.object(config, "get_settings", return_value={}):
                self.assertTrue(config.opus_filtering_enabled())
                self.assertEqual(config.opus_whitelist(), [])


class ScoreAnthropicRequiredTest(unittest.TestCase):
    @patch("clashpilot.selector.delay")
    def test_rejects_node_when_anthropic_unreachable(self, mock_delay) -> None:
        def side_effect(node: str, url: str, timeout_ms: int = 0, expected: str | None = None):
            if "anthropic" in url:
                return None
            return 100

        mock_delay.side_effect = side_effect
        self.assertIsNone(selector.score("test-node"))

    @patch("clashpilot.selector.delay", return_value=100)
    def test_accepts_node_when_all_targets_ok(self, _mock_delay) -> None:
        score = selector.score("test-node")
        self.assertIsNotNone(score)
        self.assertEqual(score, 100.0)


class HealthThresholdTest(unittest.TestCase):
    @patch.object(health, "is_alive", return_value=True)
    @patch.object(health, "anthropic_reachable", return_value=False)
    def test_anthropic_failure_uses_fast_threshold(self, _anthropic, _alive) -> None:
        unhealthy, threshold = health.health_fail_threshold("node-a")
        self.assertTrue(unhealthy)
        self.assertEqual(threshold, health.ANTHROPIC_FAIL_THRESHOLD)

    @patch.object(health, "is_alive", return_value=False)
    @patch.object(health, "anthropic_reachable", return_value=True)
    def test_general_failure_uses_default_threshold(self, _anthropic, _alive) -> None:
        unhealthy, threshold = health.health_fail_threshold("node-a")
        self.assertTrue(unhealthy)
        self.assertEqual(threshold, health.HEALTH_FAIL_THRESHOLD)


class DaemonReexportsTest(unittest.TestCase):
    def test_backward_compatible_aliases(self) -> None:
        self.assertIs(daemon._health_fail_threshold, health.health_fail_threshold)
        self.assertIs(daemon.score, selector.score)
        self.assertIs(daemon.format_scan, selector.format_scan)


class MacOSServiceTunTest(unittest.TestCase):
    @patch.object(config, "ensure_service_tun", return_value=True)
    @patch.object(config, "set_tun_enabled")
    @patch.object(config, "get_settings", return_value={})
    @patch.object(config, "_env_bool", return_value=None)
    def test_enables_tun_on_first_service_install(self, _env, _settings, set_tun, _ensure) -> None:
        with patch.object(config.sys, "platform", "darwin"):
            self.assertTrue(config.ensure_macos_service_tun())
        set_tun.assert_called_once_with(True)

    @patch.object(config, "set_tun_enabled")
    @patch.object(config, "get_settings", return_value={"tun_enabled": False})
    @patch.object(config, "_env_bool", return_value=None)
    def test_skips_when_already_configured(self, _env, _settings, set_tun) -> None:
        with patch.object(config.sys, "platform", "darwin"):
            self.assertFalse(config.ensure_macos_service_tun())
        set_tun.assert_not_called()

    @patch.object(config, "set_tun_enabled")
    @patch.object(config, "get_settings", return_value={})
    @patch.object(config, "_env_bool", return_value=None)
    def test_windows_service_tun_on_first_install(self, _env, _settings, set_tun) -> None:
        with patch.object(config.sys, "platform", "win32"):
            self.assertTrue(config.ensure_windows_tun())
        set_tun.assert_called_once_with(True)


class WindowsTunDefaultTest(unittest.TestCase):
    @patch.object(config, "get_settings", return_value={})
    @patch.object(config, "_env_bool", return_value=None)
    def test_windows_defaults_to_tun_without_saved_preference(self, _env, _settings) -> None:
        with patch.object(config.sys, "platform", "win32"):
            self.assertTrue(config.tun_enabled())

    @patch.object(config, "get_settings", return_value={"tun_enabled": False})
    @patch.object(config, "_env_bool", return_value=None)
    def test_windows_respects_explicit_system_proxy(self, _env, _settings) -> None:
        with patch.object(config.sys, "platform", "win32"):
            self.assertFalse(config.tun_enabled())

    @patch.object(config, "get_settings", return_value={})
    @patch.object(config, "_env_bool", return_value=None)
    def test_linux_defaults_to_system_proxy(self, _env, _settings) -> None:
        with patch.object(config.sys, "platform", "linux"):
            self.assertFalse(config.tun_enabled())


class SignificantlyFasterTest(unittest.TestCase):
    def test_requires_thirty_percent_improvement(self) -> None:
        self.assertFalse(selector.significantly_faster(75.0, 100.0))
        self.assertTrue(selector.significantly_faster(69.0, 100.0))
        self.assertTrue(selector.significantly_faster(70.0, 100.0))

    def test_rejects_non_positive_current_score(self) -> None:
        self.assertFalse(selector.significantly_faster(50.0, 0.0))


class PickAndSwitchDeferTest(unittest.TestCase):
    def setUp(self) -> None:
        selector._reset_faster_tracking()
        selector._LAST_SWITCH_TS = 0.0
        selector._DEFER_COUNT = 0

    @patch("clashpilot.switch_policy.should_defer_switch", return_value=True)
    @patch("clashpilot.selector._switch_to_confirmed")
    @patch("clashpilot.selector.rank_nodes", return_value=[("node-b", 60.0)])
    @patch("clashpilot.selector.drop_benched", side_effect=lambda nodes: nodes)
    @patch("clashpilot.selector.eligible_nodes", return_value=["node-b"])
    @patch("clashpilot.selector.fetch_proxies", return_value={"AUTO": {"now": "node-a"}})
    @patch("clashpilot.selector.target_group", return_value="AUTO")
    @patch.object(config, "opus_whitelist", return_value=["node-b"])
    def test_defers_whitelist_enforcement_with_active_connection(self, *_mocks) -> None:
        result = selector.pick_and_switch()
        self.assertEqual(result["action"], "deferred")
        self.assertEqual(result["reason"], "whitelist enforcement")


class PickAndSwitchSustainTest(unittest.TestCase):
    def setUp(self) -> None:
        selector._reset_faster_tracking()
        selector._LAST_SWITCH_TS = 0.0
        selector._DEFER_COUNT = 0

    @patch("clashpilot.selector.confirm_stable", return_value=True)
    @patch("clashpilot.selector.has_active_target_connection", return_value=False)
    @patch("clashpilot.selector.do_switch", return_value=True)
    @patch("clashpilot.selector.rank_nodes", return_value=[("node-b", 60.0), ("node-a", 100.0)])
    @patch("clashpilot.selector.drop_benched", side_effect=lambda nodes: nodes)
    @patch("clashpilot.selector.eligible_nodes", return_value=["node-a", "node-b"])
    @patch("clashpilot.selector.fetch_proxies", return_value={"AUTO": {"now": "node-a"}})
    @patch("clashpilot.selector.target_group", return_value="AUTO")
    def test_waits_for_sustain_before_switching(
        self, *_mocks
    ) -> None:
        result = selector.pick_and_switch()
        self.assertEqual(result["action"], "pending")
        self.assertEqual(result["best"], "node-b")

    @patch("clashpilot.selector.confirm_stable", return_value=True)
    @patch("clashpilot.selector.has_active_target_connection", return_value=False)
    @patch("clashpilot.selector.do_switch", return_value=True)
    @patch("clashpilot.selector.rank_nodes", return_value=[("node-b", 60.0), ("node-a", 100.0)])
    @patch("clashpilot.selector.drop_benched", side_effect=lambda nodes: nodes)
    @patch("clashpilot.selector.eligible_nodes", return_value=["node-a", "node-b"])
    @patch("clashpilot.selector.fetch_proxies", return_value={"AUTO": {"now": "node-a"}})
    @patch("clashpilot.selector.target_group", return_value="AUTO")
    def test_switches_after_sustain_elapsed(self, mock_switch, *_mocks) -> None:
        selector._FASTER_CANDIDATE = "node-b"
        selector._FASTER_SINCE = time.time() - selector.SWITCH_SUSTAIN_SECONDS - 1
        result = selector.pick_and_switch()
        self.assertEqual(result["action"], "switched")
        self.assertEqual(result["to"], "node-b")
        mock_switch.assert_called_once()


class FindFastNodeTest(unittest.TestCase):
    @patch("clashpilot.selector.score")
    def test_returns_first_completed_reachable_node(self, mock_score) -> None:
        def side_effect(node: str) -> float | None:
            if node == "node-b":
                return 80.0
            return None

        mock_score.side_effect = side_effect
        result = selector.find_fast_node(["node-a", "node-b", "node-c"])
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result[0], "node-b")

    @patch("clashpilot.selector.score", return_value=None)
    def test_returns_none_when_all_unreachable(self, _mock_score) -> None:
        self.assertIsNone(selector.find_fast_node(["node-a", "node-b"]))


class PickAndSwitchEmergencyTest(unittest.TestCase):
    def setUp(self) -> None:
        selector._reset_faster_tracking()
        selector._LAST_SWITCH_TS = 0.0
        selector._DEFER_COUNT = 0

    @patch("clashpilot.selector.do_switch", return_value=True)
    @patch("clashpilot.selector.find_fast_node", return_value=("node-b", 60.0))
    @patch("clashpilot.selector.drop_benched", side_effect=lambda nodes: nodes)
    @patch("clashpilot.selector.eligible_nodes", return_value=["node-a", "node-b"])
    @patch(
        "clashpilot.selector.fetch_proxies",
        return_value={"AUTO": {"now": "node-a", "type": "Selector"}},
    )
    @patch("clashpilot.selector.target_group", return_value="AUTO")
    def test_emergency_uses_fast_node_path(self, *_mocks) -> None:
        result = selector.pick_and_switch(emergency=True)
        self.assertEqual(result["action"], "switched")
        self.assertEqual(result["to"], "node-b")

    @patch("clashpilot.selector.do_switch", return_value=True)
    @patch("clashpilot.selector.find_fast_node", return_value=("node-c", 10.0))
    @patch("clashpilot.selector.find_fast_chain_node", return_value=("node-b", 60.0))
    @patch("clashpilot.selector.drop_benched", side_effect=lambda nodes: nodes)
    @patch("clashpilot.selector.eligible_nodes", return_value=["node-a", "node-b", "node-c"])
    @patch(
        "clashpilot.selector.fetch_proxies",
        return_value={"AUTO": {"now": "node-a", "type": "Selector"}},
    )
    @patch("clashpilot.selector.target_group", return_value="AUTO")
    @patch.object(config, "pinned_chain", return_value=["node-a", "node-b"])
    def test_emergency_prefers_pinned_chain_over_whole_pool_race(
        self, _pinned, *_mocks
    ) -> None:
        # node-c would "win" the whole-pool race (find_fast_node), but node-b
        # is next in the pinned chain, so it must be preferred instead.
        result = selector.pick_and_switch(emergency=True)
        self.assertEqual(result["action"], "switched")
        self.assertEqual(result["to"], "node-b")

    @patch("clashpilot.selector.do_switch", return_value=True)
    @patch("clashpilot.selector.find_fast_node", return_value=("node-c", 10.0))
    @patch("clashpilot.selector.find_fast_chain_node", return_value=None)
    @patch("clashpilot.selector.drop_benched", side_effect=lambda nodes: nodes)
    @patch("clashpilot.selector.eligible_nodes", return_value=["node-a", "node-b", "node-c"])
    @patch(
        "clashpilot.selector.fetch_proxies",
        return_value={"AUTO": {"now": "node-a", "type": "Selector"}},
    )
    @patch("clashpilot.selector.target_group", return_value="AUTO")
    @patch.object(config, "pinned_chain", return_value=["node-a", "node-b"])
    def test_emergency_falls_back_to_whole_pool_when_chain_unreachable(
        self, _pinned, *_mocks
    ) -> None:
        result = selector.pick_and_switch(emergency=True)
        self.assertEqual(result["action"], "switched")
        self.assertEqual(result["to"], "node-c")


class PinnedChainBenchBypassTest(unittest.TestCase):
    def setUp(self) -> None:
        selector._reset_faster_tracking()
        selector._LAST_SWITCH_TS = 0.0
        selector._DEFER_COUNT = 0

    @patch("clashpilot.selector.do_switch", return_value=True)
    @patch("clashpilot.selector.is_alive", return_value=False)
    @patch("clashpilot.selector.rank_nodes")
    @patch("clashpilot.selector.drop_benched", side_effect=lambda nodes: ["node-c"])
    @patch("clashpilot.selector.eligible_nodes", return_value=["node-a", "node-b", "node-c"])
    @patch(
        "clashpilot.selector.fetch_proxies",
        return_value={"AUTO": {"now": "node-b", "type": "Selector"}},
    )
    @patch("clashpilot.selector.target_group", return_value="AUTO")
    @patch.object(config, "pinned_chain", return_value=["node-a", "node-b"])
    def test_benched_chain_member_still_probed_for_restore(
        self, _pinned, _target, _fetch, _eligible, _drop_benched, mock_rank, *_mocks
    ) -> None:
        # drop_benched excludes node-a and node-b (both benched); the pinned
        # chain must still be re-added to the probe pool so recovery of
        # node-a is noticed instead of waiting out the whole bench window.
        mock_rank.side_effect = lambda candidates: [(n, 50.0) for n in candidates]
        selector.pick_and_switch()
        probed = mock_rank.call_args[0][0]
        self.assertIn("node-a", probed)
        self.assertIn("node-b", probed)


class FormatScanTest(unittest.TestCase):
    @patch("clashpilot.selector.rank_nodes", return_value=[("node-a", 120.0), ("node-b", 200.0)])
    @patch("clashpilot.selector.eligible_nodes", return_value=["node-a", "node-b"])
    @patch("clashpilot.selector.fetch_proxies", return_value={})
    @patch("clashpilot.selector.target_group", return_value="AUTO")
    @patch("clashpilot.proxy_ctrl.current_node", return_value="node-a")
    @patch("clashpilot.proxy_ctrl.current_mode", return_value="rule")
    @patch.object(config, "opus_whitelist", return_value=["node-a"])
    def test_marks_current_node(self, *_mocks) -> None:
        text = selector.format_scan(top_n=5)
        self.assertIn("*", text)
        self.assertIn("node-a  *", text)
        self.assertIn("no switch", text)


if __name__ == "__main__":
    unittest.main()
