import json
import threading
import tempfile
import time
import unittest
import urllib.request
from datetime import date
from pathlib import Path

import clash_node_monitor


class ClashNodeMonitorTests(unittest.TestCase):
    @staticmethod
    def _route_service(directory: str, **overrides):
        values = {
            "database": Path(directory) / "route.sqlite3",
            "auto_route": True,
            "route_group": "自动选择",
            "route_after_failures": 2,
            "route_cooldown_seconds": 30,
            "warning_delay_ms": 800,
        }
        values.update(overrides)
        return clash_node_monitor.MonitorService(clash_node_monitor.Settings(**values))

    def test_select_nodes_excludes_strategy_groups(self):
        proxies = {
            "Node-1": {"name": "Node-1", "type": "Tuic"},
            "Node-2": {"name": "Node-2", "type": "Tuic"},
            "自动选择": {"name": "自动选择", "type": "URLTest"},
            "负载均衡": {"name": "负载均衡", "type": "LoadBalance"},
            "DIRECT": {"name": "DIRECT", "type": "Direct"},
            "COMPATIBLE": {"name": "COMPATIBLE", "type": "Compatible"},
            "JP-1": {"name": "JP-1", "type": "Tuic"},
        }
        self.assertEqual(clash_node_monitor.select_nodes(proxies, r"^Node-"), ["Node-1", "Node-2"])
        self.assertNotIn("DIRECT", clash_node_monitor.leaf_nodes(proxies))
        self.assertNotIn("COMPATIBLE", clash_node_monitor.leaf_nodes(proxies))

    def test_parse_clash_config_reads_only_controller_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            path.write_text(
                "mixed-port: 7897\nexternal-controller: 127.0.0.1:9097\nsecret: test-secret # local\n",
                encoding="utf-8",
            )
            self.assertEqual(
                clash_node_monitor.parse_clash_config(path),
                {"external-controller": "127.0.0.1:9097", "secret": "test-secret"},
            )

    def test_store_writes_latest_and_daily_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            store = clash_node_monitor.Store(Path(directory) / "monitor.sqlite3", retention_days=30)
            now = int(time.time())
            result = clash_node_monitor.CycleResult(
                started_at=now,
                finished_at=now + 2,
                discovered_nodes=["Node-1", "Node-2"],
                measurements=[
                    clash_node_monitor.Measurement("Node-1", now + 1, "ok", delay_ms=123),
                    clash_node_monitor.Measurement("Node-2", now + 1, "timeout", error="API timeout"),
                ],
                message="1 个节点需要关注",
            )
            store.write_cycle(result)
            latest = {row["node"]: row for row in store.latest_results()}
            self.assertEqual(latest["Node-1"]["delay_ms"], 123)
            self.assertEqual(latest["Node-2"]["status"], "timeout")
            self.assertEqual(store.last_cycle()["status"], "degraded")

    def test_load_settings_uses_json_overrides(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "monitor_config.json"
            config.write_text(
                json.dumps(
                    {
                        "controller": "127.0.0.1:9999",
                        "interval_seconds": 30,
                        "node_pattern": "^Node-\\d+$",
                        "database": str(Path(directory) / "data.sqlite3"),
                    }
                ),
                encoding="utf-8",
            )
            settings = clash_node_monitor.load_settings(config)
            self.assertEqual(settings.controller, "http://127.0.0.1:9999")
            self.assertEqual(settings.interval_seconds, 30)
            self.assertEqual(settings.database, Path(directory) / "data.sqlite3")

    def test_aggregate_day_samples_marks_mixed_bucket_as_degraded(self):
        start = 1_700_000_000
        rows = [
            {"sampled_at": start + 10, "node": "Node-1", "status": "ok", "delay_ms": 100},
            {"sampled_at": start + 20, "node": "Node-1", "status": "timeout", "delay_ms": None},
        ]
        grouped = clash_node_monitor._aggregate_day_samples(rows, start)
        self.assertEqual(grouped["Node-1"][0]["status"], "degraded")
        self.assertEqual(grouped["Node-1"][0]["delay_ms"], 100)

    def test_aggregate_day_samples_accepts_one_minute_buckets(self):
        start = 1_700_000_000
        rows = [
            {"sampled_at": start + 10, "node": "Node-1", "status": "ok", "delay_ms": 100},
            {"sampled_at": start + 70, "node": "Node-1", "status": "ok", "delay_ms": 120},
        ]
        grouped = clash_node_monitor._aggregate_day_samples(rows, start, bucket_seconds=60)
        self.assertEqual([point["timestamp"] for point in grouped["Node-1"]], [start + 30, start + 90])
        self.assertEqual([point["delay_ms"] for point in grouped["Node-1"]], [100, 120])

    def test_history_payload_exposes_requested_bucket(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = clash_node_monitor.MonitorRuntime(
                clash_node_monitor.Settings(database=Path(directory) / "history.sqlite3"),
                Path(directory) / "monitor_config.json",
            )
            try:
                payload = runtime.history_payload(date(2026, 8, 31), bucket_seconds=60)
                self.assertEqual(payload["bucketSeconds"], 60)
            finally:
                runtime.stop()

    def test_route_context_uses_default_match_selector_for_nested_urltest(self):
        proxies = {
            "自动选择": {
                "name": "自动选择",
                "type": "URLTest",
                "now": "Node-1",
                "all": ["Node-1", "Node-2", "Node-3"],
            },
            "默认选择": {
                "name": "默认选择",
                "type": "Selector",
                "now": "Node-3",
                "all": ["自动选择", "Node-1", "Node-2", "Node-3"],
            },
        }
        context = clash_node_monitor.resolve_route_context(
            proxies,
            [{"type": "MATCH", "proxy": "默认选择"}],
            "自动选择",
        )
        self.assertEqual(context.control_group, "默认选择")
        self.assertEqual(context.current_node, "Node-3")
        self.assertEqual(context.path, ("默认选择",))
        self.assertEqual(context.allowed_members, ("Node-1", "Node-2", "Node-3"))

    def test_auto_route_keeps_green_current_node_even_when_ranked_second(self):
        with tempfile.TemporaryDirectory() as directory:
            service = self._route_service(directory)
            calls = []

            class FakeClient:
                def get_route_context(self, _group, timeout_seconds=6.0):
                    return clash_node_monitor.RouteContext(
                        "自动选择",
                        "urltest",
                        "默认选择",
                        "selector",
                        "Node-2",
                        ("Node-1", "Node-2", "Node-3"),
                        ("默认选择",),
                    )

                def set_proxy(self, group, node):
                    calls.append((group, node))

            service.client = FakeClient()
            try:
                result = clash_node_monitor.CycleResult(
                    started_at=1,
                    finished_at=2,
                    discovered_nodes=["Node-1", "Node-2", "Node-3"],
                    measurements=[
                        clash_node_monitor.Measurement("Node-1", 2, "ok", delay_ms=80),
                        clash_node_monitor.Measurement("Node-2", 2, "ok", delay_ms=300),
                        clash_node_monitor.Measurement("Node-3", 2, "ok", delay_ms=90),
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
            current = ["Node-1"]

            class FakeClient:
                def get_route_context(self, _group, timeout_seconds=6.0):
                    return clash_node_monitor.RouteContext(
                        "自动选择",
                        "urltest",
                        "默认选择",
                        "selector",
                        current[0],
                        ("Node-1", "Node-2", "Node-3"),
                        ("默认选择",),
                    )

                def get_proxy(self, _group):
                    return {"name": "默认选择", "type": "Selector", "now": current[0]}

                def set_proxy(self, group, node):
                    calls.append((group, node))
                    current[0] = node

            service.client = FakeClient()
            measurements = [
                clash_node_monitor.Measurement("Node-1", 2, "ok", delay_ms=1200),
                clash_node_monitor.Measurement("Node-2", 2, "ok", delay_ms=240),
                clash_node_monitor.Measurement("Node-3", 2, "ok", delay_ms=120),
            ]
            try:
                first = clash_node_monitor.CycleResult(1, 2, ["Node-1", "Node-2", "Node-3"], measurements)
                service._maybe_route(first)
                self.assertEqual(first.route_action, "waiting")
                self.assertEqual(calls, [])

                second = clash_node_monitor.CycleResult(2, 3, ["Node-1", "Node-2", "Node-3"], measurements)
                service._maybe_route(second)
                self.assertEqual(second.route_action, "switched")
                self.assertEqual(second.route_from, "Node-1")
                self.assertEqual(second.route_to, "Node-3")
                self.assertEqual(calls, [("默认选择", "Node-3")])
            finally:
                service.stop()

    def test_auto_route_switches_immediately_on_first_timeout(self):
        with tempfile.TemporaryDirectory() as directory:
            service = self._route_service(directory, route_after_failures=2)
            calls = []
            current = ["Node-1"]

            class FakeClient:
                def get_route_context(self, _group, timeout_seconds=6.0):
                    return clash_node_monitor.RouteContext(
                        "自动选择",
                        "urltest",
                        "默认选择",
                        "selector",
                        current[0],
                        ("Node-1", "Node-2"),
                        ("默认选择",),
                    )

                def get_proxy(self, _group):
                    return {"name": "默认选择", "type": "Selector", "now": current[0]}

                def set_proxy(self, group, node):
                    calls.append((group, node))
                    current[0] = node

            service.client = FakeClient()
            try:
                result = clash_node_monitor.CycleResult(
                    1,
                    2,
                    ["Node-1", "Node-2"],
                    [
                        clash_node_monitor.Measurement("Node-1", 2, "timeout"),
                        clash_node_monitor.Measurement("Node-2", 2, "ok", delay_ms=120),
                    ],
                )
                service._maybe_route(result)
                self.assertEqual(result.route_action, "switched")
                self.assertEqual(result.route_from, "Node-1")
                self.assertEqual(result.route_to, "Node-2")
                self.assertEqual(calls, [("默认选择", "Node-2")])
                self.assertIn("断线/错误", result.route_message)
            finally:
                service.stop()

    def test_auto_route_bypasses_cooldown_after_confirmed_hard_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            service = self._route_service(directory, route_after_failures=1, route_cooldown_seconds=3600)
            calls = []
            current = ["Node-1"]
            service._last_route_at = int(time.time())

            class FakeClient:
                def get_route_context(self, _group, timeout_seconds=6.0):
                    return clash_node_monitor.RouteContext(
                        "自动选择",
                        "urltest",
                        "默认选择",
                        "selector",
                        current[0],
                        ("Node-1", "Node-2"),
                        ("默认选择",),
                    )

                def get_proxy(self, _group):
                    return {"name": "默认选择", "type": "Selector", "now": current[0]}

                def set_proxy(self, group, node):
                    calls.append((group, node))
                    current[0] = node

            service.client = FakeClient()
            try:
                result = clash_node_monitor.CycleResult(
                    1,
                    2,
                    ["Node-1", "Node-2"],
                    [
                        clash_node_monitor.Measurement("Node-1", 2, "timeout"),
                        clash_node_monitor.Measurement("Node-2", 2, "ok", delay_ms=120),
                    ],
                )
                service._maybe_route(result)
                self.assertEqual(result.route_action, "switched")
                self.assertEqual(result.route_to, "Node-2")
                self.assertEqual(calls, [("默认选择", "Node-2")])
                self.assertIn("故障转移已跳过冷却", result.route_message)
            finally:
                service.stop()

    def test_auto_route_bypasses_cooldown_for_confirmed_orange_latency(self):
        with tempfile.TemporaryDirectory() as directory:
            service = self._route_service(directory, route_after_failures=1, route_cooldown_seconds=3600)
            calls = []
            current = ["Node-1"]
            service._last_route_at = int(time.time())

            class FakeClient:
                def get_route_context(self, _group, timeout_seconds=6.0):
                    return clash_node_monitor.RouteContext(
                        "自动选择",
                        "urltest",
                        "默认选择",
                        "selector",
                        current[0],
                        ("Node-1", "Node-2"),
                        ("默认选择",),
                    )

                def get_proxy(self, _group):
                    return {"name": "默认选择", "type": "Selector", "now": current[0]}

                def set_proxy(self, group, node):
                    calls.append((group, node))
                    current[0] = node

            service.client = FakeClient()
            try:
                result = clash_node_monitor.CycleResult(
                    1,
                    2,
                    ["Node-1", "Node-2"],
                    [
                        clash_node_monitor.Measurement("Node-1", 2, "ok", delay_ms=1200),
                        clash_node_monitor.Measurement("Node-2", 2, "ok", delay_ms=120),
                    ],
                )
                service._maybe_route(result)
                self.assertEqual(result.route_action, "switched")
                self.assertEqual(result.route_to, "Node-2")
                self.assertEqual(calls, [("默认选择", "Node-2")])
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
                    return clash_node_monitor.RouteContext(
                        "自动选择",
                        "urltest",
                        "默认选择",
                        "selector",
                        "Node-1",
                        ("Node-1", "Node-2"),
                        ("默认选择",),
                    )

                def set_proxy(self, group, node):
                    calls.append((group, node))

            service.client = FakeClient()
            try:
                result = clash_node_monitor.CycleResult(
                    1,
                    2,
                    ["Node-1", "Node-2"],
                    [
                        clash_node_monitor.Measurement("Node-1", 2, "timeout"),
                        clash_node_monitor.Measurement("Node-2", 2, "ok", delay_ms=1200),
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
                clash_node_monitor.RouteContext(
                    "自动选择", "urltest", "默认选择", "selector", "Node-1",
                    ("Node-1", "Node-2", "Node-3"), ("默认选择",),
                ),
                clash_node_monitor.RouteContext(
                    "自动选择", "urltest", "默认选择", "selector", "Node-2",
                    ("Node-1", "Node-2", "Node-3"), ("默认选择",),
                ),
            ]

            class FakeClient:
                def get_route_context(self, _group, timeout_seconds=6.0):
                    return contexts.pop(0) if contexts else clash_node_monitor.RouteContext(
                        "自动选择", "urltest", "默认选择", "selector", "Node-2",
                        ("Node-1", "Node-2", "Node-3"), ("默认选择",),
                    )

                def set_proxy(self, group, node):
                    calls.append((group, node))

            service.client = FakeClient()
            try:
                result = clash_node_monitor.CycleResult(
                    1,
                    2,
                    ["Node-1", "Node-2", "Node-3"],
                    [
                        clash_node_monitor.Measurement("Node-1", 2, "ok", delay_ms=1200),
                        clash_node_monitor.Measurement("Node-2", 2, "ok", delay_ms=240),
                        clash_node_monitor.Measurement("Node-3", 2, "ok", delay_ms=120),
                    ],
                )
                service._maybe_route(result)
                self.assertEqual(result.route_action, "waiting")
                self.assertEqual(calls, [])
                self.assertIn("外部改为 Node-2", result.route_message)
            finally:
                service.stop()

    def test_status_reads_current_clash_node_even_while_sampling_is_paused(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = clash_node_monitor.Settings(
                database=Path(directory) / "status.sqlite3",
                auto_route=True,
                route_group="自动选择",
            )
            runtime = clash_node_monitor.MonitorRuntime(settings, Path(directory) / "monitor_config.json")
            calls = []

            class FakeClient:
                def __init__(self, client_settings):
                    self.settings = client_settings

                def get_route_context(self, group, timeout_seconds=6.0):
                    calls.append((group, timeout_seconds))
                    return clash_node_monitor.RouteContext(
                        group,
                        "urltest",
                        "默认选择",
                        "selector",
                        "Node-5",
                        ("Node-1", "Node-5"),
                        ("默认选择",),
                    )

            runtime.service.client = FakeClient(settings)
            runtime.service.set_paused(True)
            try:
                first = runtime.status_payload()
                second = runtime.status_payload()
                self.assertTrue(first["monitor"]["paused"])
                self.assertEqual(first["routing"]["currentNode"], "Node-5")
                self.assertEqual(
                    first["routing"]["lastMessage"],
                    "已与 Clash 同步：实际出站组 默认选择 当前节点 Node-5（监控组 自动选择）",
                )
                self.assertEqual(second["routing"]["currentNode"], "Node-5")
                self.assertEqual(calls, [("自动选择", 1.5)])
            finally:
                runtime.stop()

    def test_loopback_server_rejects_a_second_monitor_on_the_same_port(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = clash_node_monitor.Settings(database=Path(directory) / "singleton.sqlite3")
            first_runtime = clash_node_monitor.MonitorRuntime(settings, Path(directory) / "monitor_config.json")
            first_server = clash_node_monitor.MonitorApiServer(("127.0.0.1", 0), first_runtime)
            second_runtime = clash_node_monitor.MonitorRuntime(settings, Path(directory) / "monitor_config.json")
            try:
                port = first_server.server_address[1]
                with self.assertRaises(OSError):
                    clash_node_monitor.MonitorApiServer(("127.0.0.1", port), second_runtime)
            finally:
                first_server.server_close()
                first_runtime.stop()
                second_runtime.stop()

    def test_loopback_server_serves_standalone_web_ui(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = clash_node_monitor.Settings(database=Path(directory) / "web.sqlite3")
            runtime = clash_node_monitor.MonitorRuntime(settings, Path(directory) / "monitor_config.json")
            server = clash_node_monitor.MonitorApiServer(("127.0.0.1", 0), runtime)
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
            settings = clash_node_monitor.Settings(database=Path(directory) / "export.sqlite3")
            runtime = clash_node_monitor.MonitorRuntime(settings, Path(directory) / "monitor_config.json")
            now = int(time.time())
            runtime.service.store.write_cycle(
                clash_node_monitor.CycleResult(
                    started_at=now,
                    finished_at=now + 1,
                    discovered_nodes=["Node-1", "Node-2"],
                    measurements=[
                        clash_node_monitor.Measurement("Node-1", now, "ok", delay_ms=123, request_ms=140),
                        clash_node_monitor.Measurement("Node-2", now, "timeout", error="API timeout"),
                    ],
                )
            )
            server = clash_node_monitor.MonitorApiServer(("127.0.0.1", 0), runtime)
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
                    self.assertIn("Node-1,ok,123,140", body)
                    self.assertIn("Node-2,timeout,,0,API timeout", body)
            finally:
                server.shutdown()
                server.server_close()
                runtime.stop()


if __name__ == "__main__":
    unittest.main()
