import json
import threading
import tempfile
import time
import unittest
import urllib.request
from datetime import date
from pathlib import Path

import tw_monitor


class TwMonitorTests(unittest.TestCase):
    @staticmethod
    def _route_service(directory: str, **overrides):
        values = {
            "database": Path(directory) / "route.sqlite3",
            "auto_route": True,
            "route_group": "TW自动选择",
            "route_after_failures": 2,
            "route_cooldown_seconds": 30,
            "warning_delay_ms": 800,
        }
        values.update(overrides)
        return tw_monitor.MonitorService(tw_monitor.Settings(**values))

    def test_select_nodes_excludes_strategy_groups(self):
        proxies = {
            "TW-1": {"name": "TW-1", "type": "Tuic"},
            "TW-2": {"name": "TW-2", "type": "Tuic"},
            "TW自动选择": {"name": "TW自动选择", "type": "URLTest"},
            "TW负载均衡": {"name": "TW负载均衡", "type": "LoadBalance"},
            "JP-1": {"name": "JP-1", "type": "Tuic"},
        }
        self.assertEqual(tw_monitor.select_nodes(proxies, r"^TW"), ["TW-1", "TW-2"])

    def test_parse_clash_config_reads_only_controller_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            path.write_text(
                "mixed-port: 7897\nexternal-controller: 127.0.0.1:9097\nsecret: test-secret # local\n",
                encoding="utf-8",
            )
            self.assertEqual(
                tw_monitor.parse_clash_config(path),
                {"external-controller": "127.0.0.1:9097", "secret": "test-secret"},
            )

    def test_store_writes_latest_and_daily_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            store = tw_monitor.Store(Path(directory) / "monitor.sqlite3", retention_days=30)
            now = int(time.time())
            result = tw_monitor.CycleResult(
                started_at=now,
                finished_at=now + 2,
                discovered_nodes=["TW-1", "TW-2"],
                measurements=[
                    tw_monitor.Measurement("TW-1", now + 1, "ok", delay_ms=123),
                    tw_monitor.Measurement("TW-2", now + 1, "timeout", error="API timeout"),
                ],
                message="1 个节点需要关注",
            )
            store.write_cycle(result)
            latest = {row["node"]: row for row in store.latest_results()}
            self.assertEqual(latest["TW-1"]["delay_ms"], 123)
            self.assertEqual(latest["TW-2"]["status"], "timeout")
            self.assertEqual(store.last_cycle()["status"], "degraded")

    def test_load_settings_uses_json_overrides(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "monitor_config.json"
            config.write_text(
                json.dumps(
                    {
                        "controller": "127.0.0.1:9999",
                        "interval_seconds": 30,
                        "node_pattern": "^TW-\\d+$",
                        "database": str(Path(directory) / "data.sqlite3"),
                    }
                ),
                encoding="utf-8",
            )
            settings = tw_monitor.load_settings(config)
            self.assertEqual(settings.controller, "http://127.0.0.1:9999")
            self.assertEqual(settings.interval_seconds, 30)
            self.assertEqual(settings.database, Path(directory) / "data.sqlite3")

    def test_aggregate_day_samples_marks_mixed_bucket_as_degraded(self):
        start = 1_700_000_000
        rows = [
            {"sampled_at": start + 10, "node": "TW-1", "status": "ok", "delay_ms": 100},
            {"sampled_at": start + 20, "node": "TW-1", "status": "timeout", "delay_ms": None},
        ]
        grouped = tw_monitor._aggregate_day_samples(rows, start)
        self.assertEqual(grouped["TW-1"][0]["status"], "degraded")
        self.assertEqual(grouped["TW-1"][0]["delay_ms"], 100)

    def test_aggregate_day_samples_accepts_one_minute_buckets(self):
        start = 1_700_000_000
        rows = [
            {"sampled_at": start + 10, "node": "TW-1", "status": "ok", "delay_ms": 100},
            {"sampled_at": start + 70, "node": "TW-1", "status": "ok", "delay_ms": 120},
        ]
        grouped = tw_monitor._aggregate_day_samples(rows, start, bucket_seconds=60)
        self.assertEqual([point["timestamp"] for point in grouped["TW-1"]], [start + 30, start + 90])
        self.assertEqual([point["delay_ms"] for point in grouped["TW-1"]], [100, 120])

    def test_history_payload_exposes_requested_bucket(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = tw_monitor.MonitorRuntime(
                tw_monitor.Settings(database=Path(directory) / "history.sqlite3"),
                Path(directory) / "monitor_config.json",
            )
            try:
                payload = runtime.history_payload(date(2026, 8, 31), bucket_seconds=60)
                self.assertEqual(payload["bucketSeconds"], 60)
            finally:
                runtime.stop()

    def test_route_context_uses_default_match_selector_for_nested_urltest(self):
        proxies = {
            "TW自动选择": {
                "name": "TW自动选择",
                "type": "URLTest",
                "now": "TW-1",
                "all": ["TW-1", "TW-2", "TW-3"],
            },
            "主代理": {
                "name": "主代理",
                "type": "Selector",
                "now": "TW-3",
                "all": ["TW自动选择", "TW-1", "TW-2", "TW-3"],
            },
        }
        context = tw_monitor.resolve_route_context(
            proxies,
            [{"type": "MATCH", "proxy": "主代理"}],
            "TW自动选择",
        )
        self.assertEqual(context.control_group, "主代理")
        self.assertEqual(context.current_node, "TW-3")
        self.assertEqual(context.path, ("主代理",))
        self.assertEqual(context.allowed_members, ("TW-1", "TW-2", "TW-3"))

    def test_auto_route_keeps_green_current_node_even_when_ranked_second(self):
        with tempfile.TemporaryDirectory() as directory:
            service = self._route_service(directory)
            calls = []

            class FakeClient:
                def get_route_context(self, _group, timeout_seconds=6.0):
                    return tw_monitor.RouteContext(
                        "TW自动选择",
                        "urltest",
                        "主代理",
                        "selector",
                        "TW-2",
                        ("TW-1", "TW-2", "TW-3"),
                        ("主代理",),
                    )

                def set_proxy(self, group, node):
                    calls.append((group, node))

            service.client = FakeClient()
            try:
                result = tw_monitor.CycleResult(
                    started_at=1,
                    finished_at=2,
                    discovered_nodes=["TW-1", "TW-2", "TW-3"],
                    measurements=[
                        tw_monitor.Measurement("TW-1", 2, "ok", delay_ms=80),
                        tw_monitor.Measurement("TW-2", 2, "ok", delay_ms=300),
                        tw_monitor.Measurement("TW-3", 2, "ok", delay_ms=90),
                    ],
                )
                service._maybe_route(result)
                self.assertEqual(result.route_action, "kept")
                self.assertEqual(calls, [])
                self.assertEqual(service.routing_snapshot()["badStreak"], 0)
            finally:
                service.stop()

    def test_auto_route_waits_for_failures_then_switches_to_fast_green_candidate(self):
        with tempfile.TemporaryDirectory() as directory:
            service = self._route_service(directory)
            calls = []
            current = ["TW-1"]

            class FakeClient:
                def get_route_context(self, _group, timeout_seconds=6.0):
                    return tw_monitor.RouteContext(
                        "TW自动选择",
                        "urltest",
                        "主代理",
                        "selector",
                        current[0],
                        ("TW-1", "TW-2", "TW-3"),
                        ("主代理",),
                    )

                def get_proxy(self, _group):
                    return {"name": "主代理", "type": "Selector", "now": current[0]}

                def set_proxy(self, group, node):
                    calls.append((group, node))
                    current[0] = node

            service.client = FakeClient()
            measurements = [
                tw_monitor.Measurement("TW-1", 2, "ok", delay_ms=1200),
                tw_monitor.Measurement("TW-2", 2, "ok", delay_ms=240),
                tw_monitor.Measurement("TW-3", 2, "ok", delay_ms=120),
            ]
            try:
                first = tw_monitor.CycleResult(1, 2, ["TW-1", "TW-2", "TW-3"], measurements)
                service._maybe_route(first)
                self.assertEqual(first.route_action, "waiting")
                self.assertEqual(calls, [])

                second = tw_monitor.CycleResult(2, 3, ["TW-1", "TW-2", "TW-3"], measurements)
                service._maybe_route(second)
                self.assertEqual(second.route_action, "switched")
                self.assertEqual(second.route_from, "TW-1")
                self.assertEqual(second.route_to, "TW-3")
                self.assertEqual(calls, [("主代理", "TW-3")])
            finally:
                service.stop()

    def test_auto_route_switches_immediately_on_first_timeout(self):
        with tempfile.TemporaryDirectory() as directory:
            service = self._route_service(directory, route_after_failures=2)
            calls = []
            current = ["TW-1"]

            class FakeClient:
                def get_route_context(self, _group, timeout_seconds=6.0):
                    return tw_monitor.RouteContext(
                        "TW自动选择",
                        "urltest",
                        "主代理",
                        "selector",
                        current[0],
                        ("TW-1", "TW-2"),
                        ("主代理",),
                    )

                def get_proxy(self, _group):
                    return {"name": "主代理", "type": "Selector", "now": current[0]}

                def set_proxy(self, group, node):
                    calls.append((group, node))
                    current[0] = node

            service.client = FakeClient()
            try:
                result = tw_monitor.CycleResult(
                    1,
                    2,
                    ["TW-1", "TW-2"],
                    [
                        tw_monitor.Measurement("TW-1", 2, "timeout"),
                        tw_monitor.Measurement("TW-2", 2, "ok", delay_ms=120),
                    ],
                )
                service._maybe_route(result)
                self.assertEqual(result.route_action, "switched")
                self.assertEqual(result.route_from, "TW-1")
                self.assertEqual(result.route_to, "TW-2")
                self.assertEqual(calls, [("主代理", "TW-2")])
                self.assertIn("断线/错误", result.route_message)
            finally:
                service.stop()

    def test_auto_route_bypasses_cooldown_after_confirmed_hard_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            service = self._route_service(directory, route_after_failures=1, route_cooldown_seconds=3600)
            calls = []
            current = ["TW-1"]
            service._last_route_at = int(time.time())

            class FakeClient:
                def get_route_context(self, _group, timeout_seconds=6.0):
                    return tw_monitor.RouteContext(
                        "TW自动选择",
                        "urltest",
                        "主代理",
                        "selector",
                        current[0],
                        ("TW-1", "TW-2"),
                        ("主代理",),
                    )

                def get_proxy(self, _group):
                    return {"name": "主代理", "type": "Selector", "now": current[0]}

                def set_proxy(self, group, node):
                    calls.append((group, node))
                    current[0] = node

            service.client = FakeClient()
            try:
                result = tw_monitor.CycleResult(
                    1,
                    2,
                    ["TW-1", "TW-2"],
                    [
                        tw_monitor.Measurement("TW-1", 2, "timeout"),
                        tw_monitor.Measurement("TW-2", 2, "ok", delay_ms=120),
                    ],
                )
                service._maybe_route(result)
                self.assertEqual(result.route_action, "switched")
                self.assertEqual(result.route_to, "TW-2")
                self.assertEqual(calls, [("主代理", "TW-2")])
                self.assertIn("故障转移已跳过冷却", result.route_message)
            finally:
                service.stop()

    def test_auto_route_bypasses_cooldown_for_confirmed_orange_latency(self):
        with tempfile.TemporaryDirectory() as directory:
            service = self._route_service(directory, route_after_failures=1, route_cooldown_seconds=3600)
            calls = []
            current = ["TW-1"]
            service._last_route_at = int(time.time())

            class FakeClient:
                def get_route_context(self, _group, timeout_seconds=6.0):
                    return tw_monitor.RouteContext(
                        "TW自动选择",
                        "urltest",
                        "主代理",
                        "selector",
                        current[0],
                        ("TW-1", "TW-2"),
                        ("主代理",),
                    )

                def get_proxy(self, _group):
                    return {"name": "主代理", "type": "Selector", "now": current[0]}

                def set_proxy(self, group, node):
                    calls.append((group, node))
                    current[0] = node

            service.client = FakeClient()
            try:
                result = tw_monitor.CycleResult(
                    1,
                    2,
                    ["TW-1", "TW-2"],
                    [
                        tw_monitor.Measurement("TW-1", 2, "ok", delay_ms=1200),
                        tw_monitor.Measurement("TW-2", 2, "ok", delay_ms=120),
                    ],
                )
                service._maybe_route(result)
                self.assertEqual(result.route_action, "switched")
                self.assertEqual(result.route_to, "TW-2")
                self.assertEqual(calls, [("主代理", "TW-2")])
                self.assertIn("橙色延迟", result.route_message)
                self.assertIn("故障转移已跳过冷却", result.route_message)
            finally:
                service.stop()

    def test_auto_route_does_not_switch_without_green_candidate(self):
        with tempfile.TemporaryDirectory() as directory:
            service = self._route_service(directory, route_after_failures=1)
            calls = []

            class FakeClient:
                def get_route_context(self, _group, timeout_seconds=6.0):
                    return tw_monitor.RouteContext(
                        "TW自动选择",
                        "urltest",
                        "主代理",
                        "selector",
                        "TW-1",
                        ("TW-1", "TW-2"),
                        ("主代理",),
                    )

                def set_proxy(self, group, node):
                    calls.append((group, node))

            service.client = FakeClient()
            try:
                result = tw_monitor.CycleResult(
                    1,
                    2,
                    ["TW-1", "TW-2"],
                    [
                        tw_monitor.Measurement("TW-1", 2, "timeout"),
                        tw_monitor.Measurement("TW-2", 2, "ok", delay_ms=1200),
                    ],
                )
                service._maybe_route(result)
                self.assertEqual(result.route_action, "unavailable")
                self.assertEqual(calls, [])
            finally:
                service.stop()

    def test_auto_route_does_not_overwrite_a_manual_clash_change_before_put(self):
        with tempfile.TemporaryDirectory() as directory:
            service = self._route_service(directory, route_after_failures=1)
            calls = []
            contexts = [
                tw_monitor.RouteContext(
                    "TW自动选择", "urltest", "主代理", "selector", "TW-1",
                    ("TW-1", "TW-2", "TW-3"), ("主代理",),
                ),
                tw_monitor.RouteContext(
                    "TW自动选择", "urltest", "主代理", "selector", "TW-2",
                    ("TW-1", "TW-2", "TW-3"), ("主代理",),
                ),
            ]

            class FakeClient:
                def get_route_context(self, _group, timeout_seconds=6.0):
                    return contexts.pop(0) if contexts else tw_monitor.RouteContext(
                        "TW自动选择", "urltest", "主代理", "selector", "TW-2",
                        ("TW-1", "TW-2", "TW-3"), ("主代理",),
                    )

                def set_proxy(self, group, node):
                    calls.append((group, node))

            service.client = FakeClient()
            try:
                result = tw_monitor.CycleResult(
                    1,
                    2,
                    ["TW-1", "TW-2", "TW-3"],
                    [
                        tw_monitor.Measurement("TW-1", 2, "ok", delay_ms=1200),
                        tw_monitor.Measurement("TW-2", 2, "ok", delay_ms=240),
                        tw_monitor.Measurement("TW-3", 2, "ok", delay_ms=120),
                    ],
                )
                service._maybe_route(result)
                self.assertEqual(result.route_action, "waiting")
                self.assertEqual(calls, [])
                self.assertIn("外部改为 TW-2", result.route_message)
            finally:
                service.stop()

    def test_status_reads_current_clash_node_even_while_sampling_is_paused(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = tw_monitor.Settings(
                database=Path(directory) / "status.sqlite3",
                auto_route=True,
                route_group="TW自动选择",
            )
            runtime = tw_monitor.MonitorRuntime(settings, Path(directory) / "monitor_config.json")
            calls = []

            class FakeClient:
                def __init__(self, client_settings):
                    self.settings = client_settings

                def get_route_context(self, group, timeout_seconds=6.0):
                    calls.append((group, timeout_seconds))
                    return tw_monitor.RouteContext(
                        group,
                        "urltest",
                        "主代理",
                        "selector",
                        "TW-5",
                        ("TW-1", "TW-5"),
                        ("主代理",),
                    )

            runtime.service.client = FakeClient(settings)
            runtime.service.set_paused(True)
            try:
                first = runtime.status_payload()
                second = runtime.status_payload()
                self.assertTrue(first["monitor"]["paused"])
                self.assertEqual(first["routing"]["currentNode"], "TW-5")
                self.assertEqual(
                    first["routing"]["lastMessage"],
                    "已与 Clash 同步：实际出站组 主代理 当前节点 TW-5（监控组 TW自动选择）",
                )
                self.assertEqual(second["routing"]["currentNode"], "TW-5")
                self.assertEqual(calls, [("TW自动选择", 1.5)])
            finally:
                runtime.stop()

    def test_loopback_server_rejects_a_second_monitor_on_the_same_port(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = tw_monitor.Settings(database=Path(directory) / "singleton.sqlite3")
            first_runtime = tw_monitor.MonitorRuntime(settings, Path(directory) / "monitor_config.json")
            first_server = tw_monitor.MonitorApiServer(("127.0.0.1", 0), first_runtime)
            second_runtime = tw_monitor.MonitorRuntime(settings, Path(directory) / "monitor_config.json")
            try:
                port = first_server.server_address[1]
                with self.assertRaises(OSError):
                    tw_monitor.MonitorApiServer(("127.0.0.1", port), second_runtime)
            finally:
                first_server.server_close()
                first_runtime.stop()
                second_runtime.stop()

    def test_loopback_server_serves_standalone_web_ui(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = tw_monitor.Settings(database=Path(directory) / "web.sqlite3")
            runtime = tw_monitor.MonitorRuntime(settings, Path(directory) / "monitor_config.json")
            server = tw_monitor.MonitorApiServer(("127.0.0.1", 0), runtime)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                base = f"http://127.0.0.1:{server.server_address[1]}"
                with urllib.request.urlopen(f"{base}/", timeout=2) as response:
                    body = response.read().decode("utf-8")
                    self.assertEqual(response.status, 200)
                    self.assertIn("节点质量", body)
                    self.assertIn("/app.js", body)
                with urllib.request.urlopen(f"{base}/styles.css", timeout=2) as response:
                    self.assertEqual(response.status, 200)
                    self.assertIn("text/css", response.headers.get("Content-Type", ""))
            finally:
                server.shutdown()
                server.server_close()
                runtime.stop()

    def test_loopback_server_exports_raw_samples_as_csv(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = tw_monitor.Settings(database=Path(directory) / "export.sqlite3")
            runtime = tw_monitor.MonitorRuntime(settings, Path(directory) / "monitor_config.json")
            now = int(time.time())
            runtime.service.store.write_cycle(
                tw_monitor.CycleResult(
                    started_at=now,
                    finished_at=now + 1,
                    discovered_nodes=["TW-1", "TW-2"],
                    measurements=[
                        tw_monitor.Measurement("TW-1", now, "ok", delay_ms=123, request_ms=140),
                        tw_monitor.Measurement("TW-2", now, "timeout", error="API timeout"),
                    ],
                )
            )
            server = tw_monitor.MonitorApiServer(("127.0.0.1", 0), runtime)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                url = f"http://127.0.0.1:{server.server_address[1]}/api/export?days=1"
                with urllib.request.urlopen(url, timeout=2) as response:
                    body = response.read().decode("utf-8-sig")
                    self.assertEqual(response.status, 200)
                    self.assertIn("text/csv", response.headers.get("Content-Type", ""))
                    self.assertEqual(response.headers.get("X-Export-Row-Count"), "2")
                    self.assertIn("sampled_at_epoch,sampled_at_local", body)
                    self.assertIn("TW-1,ok,123,140", body)
                    self.assertIn("TW-2,timeout,,0,API timeout", body)
            finally:
                server.shutdown()
                server.server_close()
                runtime.stop()


if __name__ == "__main__":
    unittest.main()
