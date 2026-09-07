"""Lightweight Clash Verge/mihomo node monitor.

The program intentionally uses only the Python standard library.  It talks to
Clash's local External Controller instead of clicking the Clash Verge UI, so
the same sampler can run with or without the dashboard window.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import csv
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import math
import os
import queue
import re
import socket
import sqlite3
import statistics
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time as datetime_time, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


APP_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
RESOURCE_ROOT = Path(getattr(sys, "_MEIPASS", APP_DIR))
WEB_ROOT = RESOURCE_ROOT / "web"
DEFAULT_CONFIG_PATH = APP_DIR / "monitor_config.json"
DEFAULT_DATABASE_PATH = APP_DIR / "data" / "node_monitor.sqlite3"
DEFAULT_CONTROLLER = "http://127.0.0.1:9097"
DEFAULT_PATTERN = r".*"
DEFAULT_TEST_URL = "http://www.gstatic.com/generate_204"
DEFAULT_SERVER_HOST = "127.0.0.1"
DEFAULT_SERVER_PORT = 17997
API_VERSION = "clash-node-monitor-api-v1"
DEFAULT_ROUTE_GROUP = ""

STATIC_MIME_TYPES = {
    ".css": "text/css; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
}

GROUP_TYPES = {
    "selector",
    "urltest",
    "fallback",
    "loadbalance",
    "load-balance",
    "relay",
    "smart",
    "load-balance-urltest",
}

# Clash exposes several selectable control entries alongside real proxy
# leaves. They are useful to the Clash UI but are not meaningful latency
# targets for a node monitor.
NON_MONITORABLE_TYPES = {
    "direct",
    "reject",
    "reject-drop",
    "rejectdrop",
    "pass",
    "pass-rule",
    "compatible",
    "dns",
}
NON_MONITORABLE_NAMES = {
    "DIRECT",
    "REJECT",
    "REJECT-DROP",
    "PASS",
    "PASS-RULE",
    "COMPATIBLE",
    "DNS",
}


def _positive_int(value: Any, fallback: int, minimum: int, maximum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return fallback
    return max(minimum, min(maximum, number))


def _yaml_scalar(value: str) -> str:
    """Read a simple YAML scalar without requiring PyYAML.

    Clash's controller and secret are flat scalar fields.  This deliberately
    does not attempt to become a general YAML parser.
    """

    value = value.strip()
    if " #" in value:
        value = value.split(" #", 1)[0].rstrip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        value = value[1:-1]
    return value.strip()


def parse_clash_config(path: Optional[Path]) -> Dict[str, str]:
    if not path or not path.is_file():
        return {}
    try:
        text = path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return {}

    values: Dict[str, str] = {}
    for line in text.splitlines():
        match = re.match(r"^\s*(external-controller|secret)\s*:\s*(.*?)\s*$", line)
        if match:
            values[match.group(1)] = _yaml_scalar(match.group(2))
    return values


def discover_clash_config(explicit: Optional[str] = None) -> Optional[Path]:
    candidates: List[Path] = []
    if explicit:
        candidates.append(Path(os.path.expandvars(os.path.expanduser(explicit))))
    env_path = os.environ.get("CLASH_CONFIG_PATH")
    if env_path:
        candidates.append(Path(os.path.expandvars(os.path.expanduser(env_path))))

    appdata = os.environ.get("APPDATA")
    if appdata:
        appdata_path = Path(appdata)
        candidates.extend(
            [
                appdata_path
                / "io.github.clash-verge-rev.clash-verge-rev"
                / "config.yaml",
                appdata_path / "clash-verge-rev" / "config.yaml",
                appdata_path / "clash-verge" / "config.yaml",
                appdata_path / "mihomo" / "config.yaml",
            ]
        )

    user_profile = Path.home()
    candidates.extend(
        [
            user_profile / ".config" / "clash" / "config.yaml",
            user_profile / ".config" / "mihomo" / "config.yaml",
        ]
    )

    seen: set[str] = set()
    for candidate in candidates:
        key = os.path.normcase(os.path.abspath(str(candidate)))
        if key in seen:
            continue
        seen.add(key)
        if candidate.is_file():
            return candidate
    return None


def _normalise_controller(value: str) -> str:
    value = value.strip().rstrip("/")
    if not value:
        return DEFAULT_CONTROLLER
    if "://" not in value:
        value = "http://" + value
    return value.rstrip("/")


@dataclass(frozen=True)
class Settings:
    controller: str = DEFAULT_CONTROLLER
    secret: str = ""
    clash_config: Optional[str] = None
    interval_seconds: int = 60
    timeout_ms: int = 5000
    test_url: str = DEFAULT_TEST_URL
    node_pattern: str = DEFAULT_PATTERN
    workers: int = 4
    retention_days: int = 30
    database: Path = DEFAULT_DATABASE_PATH
    always_on_top: bool = True
    selected_nodes: Tuple[str, ...] = ()
    server_host: str = DEFAULT_SERVER_HOST
    server_port: int = DEFAULT_SERVER_PORT
    auto_route: bool = False
    route_group: str = DEFAULT_ROUTE_GROUP
    route_after_failures: int = 2
    route_cooldown_seconds: int = 300
    warning_delay_ms: int = 800


def load_settings(config_path: Optional[Path] = None) -> Settings:
    config_path = config_path or DEFAULT_CONFIG_PATH
    raw: Dict[str, Any] = {}
    if config_path.is_file():
        try:
            loaded = json.loads(config_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                raw = loaded
        except (OSError, json.JSONDecodeError):
            raw = {}

    configured_clash_path = raw.get("clash_config") or None
    clash_path = discover_clash_config(str(configured_clash_path) if configured_clash_path else None)
    clash_values = parse_clash_config(clash_path)

    controller_value = raw.get("controller") or clash_values.get("external-controller") or DEFAULT_CONTROLLER
    secret_value = (
        raw.get("secret")
        or os.environ.get("CLASH_SECRET")
        or clash_values.get("secret")
        or ""
    )

    database_value = raw.get("database") or str(DEFAULT_DATABASE_PATH)
    database = Path(os.path.expandvars(os.path.expanduser(str(database_value))))
    if not database.is_absolute():
        database = APP_DIR / database

    pattern = str(raw.get("node_pattern") or DEFAULT_PATTERN)
    try:
        re.compile(pattern)
    except re.error:
        pattern = DEFAULT_PATTERN

    test_url = str(raw.get("test_url") or DEFAULT_TEST_URL).strip()
    if not test_url.startswith(("http://", "https://")):
        test_url = DEFAULT_TEST_URL

    configured_path_text = str(configured_clash_path) if configured_clash_path else str(clash_path) if clash_path else None
    selected_raw = raw.get("selected_nodes")
    selected_nodes: Tuple[str, ...] = ()
    if isinstance(selected_raw, list):
        selected_nodes = tuple(
            dict.fromkeys(
                str(item).strip() for item in selected_raw
                if isinstance(item, str) and item.strip()
            )
        )[:128]
    route_group = str(raw.get("route_group") or DEFAULT_ROUTE_GROUP).strip()[:120]
    return Settings(
        controller=_normalise_controller(str(controller_value)),
        secret=str(secret_value).strip(),
        clash_config=configured_path_text,
        interval_seconds=_positive_int(raw.get("interval_seconds"), 60, 10, 86400),
        timeout_ms=_positive_int(raw.get("timeout_ms"), 5000, 1000, 30000),
        test_url=test_url,
        node_pattern=pattern,
        workers=_positive_int(raw.get("workers"), 4, 1, 8),
        retention_days=_positive_int(raw.get("retention_days"), 30, 1, 3650),
        database=database,
        always_on_top=bool(raw.get("always_on_top", True)),
        selected_nodes=selected_nodes,
        server_host=str(raw.get("server_host") or DEFAULT_SERVER_HOST).strip() or DEFAULT_SERVER_HOST,
        server_port=_positive_int(raw.get("server_port"), DEFAULT_SERVER_PORT, 1024, 65535),
        auto_route=bool(raw.get("auto_route", False)),
        route_group=route_group,
        route_after_failures=_positive_int(raw.get("route_after_failures"), 2, 1, 5),
        route_cooldown_seconds=_positive_int(raw.get("route_cooldown_seconds"), 300, 30, 3600),
        warning_delay_ms=_positive_int(raw.get("warning_delay_ms"), 800, 100, 10000),
    )


def natural_node_key(name: str) -> Tuple[Any, ...]:
    return tuple(int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", name))


def _normalise_proxy_type(value: Any) -> str:
    return str(value or "").lower().replace(" ", "").replace("_", "-")


def _is_monitorable_leaf(name: str, proxy_type: str) -> bool:
    return (
        bool(name)
        and proxy_type not in GROUP_TYPES
        and proxy_type not in NON_MONITORABLE_TYPES
        and name.strip().upper() not in NON_MONITORABLE_NAMES
    )


def _proxy_group_map(proxies: Mapping[str, Any]) -> Dict[str, Mapping[str, Any]]:
    groups: Dict[str, Mapping[str, Any]] = {}
    for key, proxy in proxies.items():
        if not isinstance(proxy, Mapping):
            continue
        name = str(proxy.get("name") or key).strip()
        if name and _normalise_proxy_type(proxy.get("type")) in GROUP_TYPES:
            groups[name] = proxy
    return groups


def _proxy_members(proxy: Mapping[str, Any]) -> Tuple[str, ...]:
    members = proxy.get("all")
    if not isinstance(members, list):
        return ()
    return tuple(dict.fromkeys(
        str(item).strip() for item in members
        if isinstance(item, str) and item.strip()
    ))


def _group_can_route_to(
    proxy: Mapping[str, Any],
    requested_group: str,
    allowed_members: Sequence[str],
) -> bool:
    members = set(_proxy_members(proxy))
    return requested_group in members or bool(members.intersection(allowed_members))


@dataclass(frozen=True)
class RouteContext:
    """The route that actually controls the default outbound traffic."""

    requested_group: str
    requested_type: str
    control_group: str
    control_type: str
    current_node: str
    allowed_members: Tuple[str, ...] = ()
    path: Tuple[str, ...] = ()


def resolve_route_context(
    proxies: Mapping[str, Any],
    rules: Sequence[Mapping[str, Any]],
    requested_group: str,
) -> RouteContext:
    """Resolve a monitor group to the Selector used by Clash's active rules.

    A URLTest group such as ``自动选择`` reports its own ``now`` value, but
    it is not necessarily the group that the default ``MATCH`` rule sends
    traffic through.  Writing to that nested group can therefore leave the
    user's real outbound Selector unchanged.  Prefer the Selector referenced
    by the live MATCH rule and only fall back to a matching Selector when the
    configured group itself is not manually selectable.
    """

    requested_group = str(requested_group or "").strip()
    groups = _proxy_group_map(proxies)
    requested_payload = groups.get(requested_group)
    if requested_payload is None:
        raise ClashApiError(f"Clash 中不存在路由组“{requested_group}”")
    requested_type = _normalise_proxy_type(requested_payload.get("type"))
    allowed_members = _proxy_members(requested_payload)
    if not allowed_members:
        raise ClashApiError(f"路由组“{requested_group}”没有可核验的节点成员")

    control_group = requested_group if requested_type == "selector" else ""
    if not control_group:
        # The last MATCH rule is the default path for ordinary traffic.  Use
        # it first so service-specific Selectors do not win by accident.
        for rule in reversed(list(rules)):
            if not isinstance(rule, Mapping):
                continue
            if str(rule.get("type") or "").strip().lower() != "match":
                continue
            candidate = str(rule.get("proxy") or "").strip()
            payload = groups.get(candidate)
            if payload and _normalise_proxy_type(payload.get("type")) == "selector" and _group_can_route_to(payload, requested_group, allowed_members):
                control_group = candidate
                break

    if not control_group:
        # Some controller versions omit a typed MATCH entry.  In that case
        # inspect rule targets from the end, preserving their configured
        # priority, and accept only a Selector that can carry the monitored
        # members.
        for rule in reversed(list(rules)):
            if not isinstance(rule, Mapping):
                continue
            candidate = str(rule.get("proxy") or "").strip()
            payload = groups.get(candidate)
            if payload and _normalise_proxy_type(payload.get("type")) == "selector" and _group_can_route_to(payload, requested_group, allowed_members):
                control_group = candidate
                break

    if not control_group:
        raise ClashApiError(
            f"路由组“{requested_group}”是 {requested_type or '未知类型'}，无法确认实际出站 Selector；为避免假切换，本轮不改 Clash"
        )

    control_payload = groups.get(control_group)
    if control_payload is None or _normalise_proxy_type(control_payload.get("type")) != "selector":
        raise ClashApiError(f"实际出站组“{control_group}”不是可手动选择的 Selector")

    current = ""
    path: List[str] = []
    current_group = control_group
    seen: set[str] = set()
    while current_group and current_group not in seen:
        seen.add(current_group)
        path.append(current_group)
        payload = groups.get(current_group)
        if payload is None:
            break
        current = str(payload.get("now") or "").strip()
        if current in groups:
            current_group = current
            continue
        break

    if not current:
        raise ClashApiError(f"实际出站组“{control_group}”没有返回当前节点")

    return RouteContext(
        requested_group=requested_group,
        requested_type=requested_type,
        control_group=control_group,
        control_type=_normalise_proxy_type(control_payload.get("type")),
        current_node=current,
        allowed_members=allowed_members,
        path=tuple(path),
    )


def leaf_nodes(proxies: Mapping[str, Any]) -> List[str]:
    """Return every selectable proxy, including non-nodes for configuration."""

    nodes: List[str] = []
    for key, proxy in proxies.items():
        if not isinstance(proxy, Mapping):
            continue
        name = str(proxy.get("name") or key).strip()
        proxy_type = _normalise_proxy_type(proxy.get("type"))
        if _is_monitorable_leaf(name, proxy_type):
            nodes.append(name)
    return sorted(set(nodes), key=natural_node_key)


def select_nodes(
    proxies: Mapping[str, Any],
    pattern: str,
    selected_nodes: Sequence[str] = (),
) -> List[str]:
    compiled = re.compile(pattern)
    selected = set(selected_nodes)
    nodes: List[str] = []
    for key, proxy in proxies.items():
        if not isinstance(proxy, Mapping):
            continue
        name = str(proxy.get("name") or key)
        proxy_type = _normalise_proxy_type(proxy.get("type"))
        if _is_monitorable_leaf(name, proxy_type) and ((selected and name in selected) or (not selected and compiled.search(name))):
            nodes.append(name)
    return sorted(set(nodes), key=natural_node_key)


def _short_error(error: str, limit: int = 180) -> str:
    cleaned = " ".join(str(error).split())
    if len(cleaned) > limit:
        return cleaned[: limit - 1] + "…"
    return cleaned


@dataclass
class Measurement:
    node: str
    sampled_at: int
    status: str
    delay_ms: Optional[int] = None
    error: str = ""
    request_ms: int = 0


@dataclass
class CycleResult:
    started_at: int
    finished_at: int
    discovered_nodes: List[str] = field(default_factory=list)
    measurements: List[Measurement] = field(default_factory=list)
    message: str = ""
    route_action: str = "none"
    route_from: str = ""
    route_to: str = ""
    route_message: str = ""
    route_group: str = ""

    @property
    def ok_count(self) -> int:
        return sum(item.status == "ok" for item in self.measurements)

    @property
    def timeout_count(self) -> int:
        return sum(item.status == "timeout" for item in self.measurements)

    @property
    def error_count(self) -> int:
        return sum(item.status == "error" for item in self.measurements)

    @property
    def duration_ms(self) -> int:
        return max(0, (self.finished_at - self.started_at) * 1000)

    @property
    def cycle_status(self) -> str:
        if self.message and not self.measurements:
            return "error"
        if self.timeout_count or self.error_count:
            return "degraded"
        return "ok"


class ClashApiError(RuntimeError):
    def __init__(
        self,
        message: str,
        status_code: Optional[int] = None,
        timed_out: bool = False,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.timed_out = timed_out


class ClashClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def _request_json(
        self,
        path: str,
        params: Optional[Mapping[str, Any]] = None,
        timeout_seconds: float = 8.0,
        method: str = "GET",
        body: Optional[Mapping[str, Any]] = None,
        allow_empty: bool = False,
    ) -> Any:
        query = urllib.parse.urlencode(params or {})
        url = f"{self.settings.controller}{path}"
        if query:
            url += "?" + query
        headers = {"Accept": "application/json"}
        if self.settings.secret:
            headers["Authorization"] = f"Bearer {self.settings.secret}"
        data: Optional[bytes] = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, headers=headers, method=method)

        try:
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                payload = response.read()
        except urllib.error.HTTPError as exc:
            try:
                exc.read(512)
            except OSError:
                pass
            timed_out = exc.code in {408, 504}
            if exc.code == 401:
                message = "API 认证失败（请检查 secret）"
            elif timed_out:
                message = f"API 超时（HTTP {exc.code}）"
            else:
                message = f"API 返回 HTTP {exc.code}"
            raise ClashApiError(message, status_code=exc.code, timed_out=timed_out) from exc
        except (TimeoutError, OSError, urllib.error.URLError) as exc:
            raise ClashApiError("连接 Clash 控制器失败", timed_out=True) from exc

        if not payload:
            if allow_empty:
                return {}
            raise ClashApiError("Clash API 返回了空响应")

        try:
            return json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ClashApiError("Clash API 返回了无法解析的数据") from exc

    def _list_proxy_names(self) -> List[str]:
        return leaf_nodes(self.get_proxies(timeout_seconds=6.0))

    def get_proxies(self, timeout_seconds: float = 6.0) -> Mapping[str, Any]:
        payload = self._request_json("/proxies", timeout_seconds=timeout_seconds)
        if not isinstance(payload, Mapping) or not isinstance(payload.get("proxies"), Mapping):
            raise ClashApiError("Clash API 缺少 proxies 节点清单")
        return payload["proxies"]

    def get_rules(self, timeout_seconds: float = 6.0) -> List[Mapping[str, Any]]:
        payload = self._request_json("/rules", timeout_seconds=timeout_seconds)
        if not isinstance(payload, Mapping) or not isinstance(payload.get("rules"), list):
            raise ClashApiError("Clash API 缺少 rules 路由规则")
        return [item for item in payload["rules"] if isinstance(item, Mapping)]

    def get_route_context(self, requested_group: str, timeout_seconds: float = 6.0) -> RouteContext:
        proxies = self.get_proxies(timeout_seconds=timeout_seconds)
        groups = _proxy_group_map(proxies)
        requested = groups.get(str(requested_group or "").strip())
        if requested is None:
            raise ClashApiError(f"Clash 中不存在路由组“{requested_group}”")
        requested_type = _normalise_proxy_type(requested.get("type"))
        rules = self.get_rules(timeout_seconds=timeout_seconds) if requested_type != "selector" else []
        return resolve_route_context(proxies, rules, requested_group)

    def list_available_nodes(self) -> List[str]:
        return self._list_proxy_names()

    def list_nodes(self) -> List[str]:
        available = self._list_proxy_names()
        nodes = select_nodes(
            {name: {"name": name, "type": "leaf"} for name in available},
            self.settings.node_pattern,
            self.settings.selected_nodes,
        )
        if not nodes:
            if self.settings.selected_nodes:
                raise ClashApiError("选中的节点当前不在 Clash 清单中")
            raise ClashApiError(f"没有匹配到节点：{self.settings.node_pattern}")
        return nodes

    def get_proxy(self, name: str, timeout_seconds: float = 6.0) -> Mapping[str, Any]:
        path = "/proxies/" + urllib.parse.quote(name, safe="")
        payload = self._request_json(path, timeout_seconds=timeout_seconds)
        if not isinstance(payload, Mapping):
            raise ClashApiError(f"路由组 {name} 返回格式异常")
        return payload

    def set_proxy(self, group: str, node: str) -> None:
        path = "/proxies/" + urllib.parse.quote(group, safe="")
        self._request_json(
            path,
            timeout_seconds=8.0,
            method="PUT",
            body={"name": node},
            allow_empty=True,
        )

    def test_node(self, node: str) -> Measurement:
        started = time.perf_counter()
        path = "/proxies/" + urllib.parse.quote(node, safe="") + "/delay"
        params = {"url": self.settings.test_url, "timeout": self.settings.timeout_ms}
        timeout_seconds = max(8.0, self.settings.timeout_ms / 1000.0 + 3.0)
        try:
            payload = self._request_json(path, params, timeout_seconds=timeout_seconds)
            if not isinstance(payload, Mapping):
                raise ClashApiError("延迟接口返回格式异常")
            raw_delay = payload.get("delay", payload.get("latency"))
            if raw_delay is None:
                raise ClashApiError("延迟接口未返回 delay")
            delay = int(float(raw_delay))
            if delay < 0:
                raise ClashApiError("延迟接口返回了负数")
            return Measurement(
                node=node,
                sampled_at=int(time.time()),
                status="ok",
                delay_ms=delay,
                request_ms=int((time.perf_counter() - started) * 1000),
            )
        except ClashApiError as exc:
            status = "timeout" if exc.timed_out or exc.status_code in {408, 504} else "error"
            return Measurement(
                node=node,
                sampled_at=int(time.time()),
                status=status,
                error=_short_error(str(exc)),
                request_ms=int((time.perf_counter() - started) * 1000),
            )
        except (TypeError, ValueError) as exc:
            return Measurement(
                node=node,
                sampled_at=int(time.time()),
                status="error",
                error=_short_error(f"结果解析失败：{exc}"),
                request_ms=int((time.perf_counter() - started) * 1000),
            )


class Store:
    def __init__(self, path: Path, retention_days: int) -> None:
        self.path = path
        self.retention_days = retention_days
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialise()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.path), timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    @contextmanager
    def _connection(self) -> Iterable[sqlite3.Connection]:
        connection = self._connect()
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialise(self) -> None:
        with self._connection() as connection:
            connection.execute("PRAGMA synchronous = NORMAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS samples (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    sampled_at INTEGER NOT NULL,
                    local_date TEXT NOT NULL,
                    node TEXT NOT NULL,
                    status TEXT NOT NULL,
                    delay_ms INTEGER,
                    error TEXT NOT NULL DEFAULT '',
                    request_ms INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_samples_node_time
                    ON samples(node, sampled_at);
                CREATE INDEX IF NOT EXISTS idx_samples_time
                    ON samples(sampled_at);
                CREATE TABLE IF NOT EXISTS cycles (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    started_at INTEGER NOT NULL,
                    finished_at INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    node_count INTEGER NOT NULL,
                    ok_count INTEGER NOT NULL,
                    timeout_count INTEGER NOT NULL,
                    error_count INTEGER NOT NULL,
                    message TEXT NOT NULL DEFAULT '',
                    route_action TEXT NOT NULL DEFAULT 'none',
                    route_from TEXT NOT NULL DEFAULT '',
                    route_to TEXT NOT NULL DEFAULT '',
                    route_message TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_cycles_time
                    ON cycles(finished_at);
                CREATE TABLE IF NOT EXISTS route_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    switched_at INTEGER NOT NULL,
                    cycle_id INTEGER NOT NULL,
                    group_name TEXT NOT NULL,
                    from_node TEXT NOT NULL,
                    to_node TEXT NOT NULL,
                    reason TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_route_events_time
                    ON route_events(switched_at);
                """
            )
            cycle_columns = {
                str(row[1]) for row in connection.execute("PRAGMA table_info(cycles)").fetchall()
            }
            for name, definition in (
                ("route_action", "TEXT NOT NULL DEFAULT 'none'"),
                ("route_from", "TEXT NOT NULL DEFAULT ''"),
                ("route_to", "TEXT NOT NULL DEFAULT ''"),
                ("route_message", "TEXT NOT NULL DEFAULT ''"),
            ):
                if name not in cycle_columns:
                    connection.execute(f"ALTER TABLE cycles ADD COLUMN {name} {definition}")

    @staticmethod
    def _row_dict(row: Optional[sqlite3.Row]) -> Optional[Dict[str, Any]]:
        return dict(row) if row is not None else None

    def write_cycle(self, result: CycleResult) -> int:
        with self._connection() as connection:
            for measurement in result.measurements:
                local_date = datetime.fromtimestamp(measurement.sampled_at).strftime("%Y-%m-%d")
                connection.execute(
                    """
                    INSERT INTO samples
                        (sampled_at, local_date, node, status, delay_ms, error, request_ms)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        measurement.sampled_at,
                        local_date,
                        measurement.node,
                        measurement.status,
                        measurement.delay_ms,
                        measurement.error,
                        measurement.request_ms,
                    ),
                )
            connection.execute(
                """
                INSERT INTO cycles
                    (started_at, finished_at, status, node_count, ok_count,
                     timeout_count, error_count, message, route_action,
                     route_from, route_to, route_message)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    result.started_at,
                    result.finished_at,
                    result.cycle_status,
                    len(result.discovered_nodes),
                    result.ok_count,
                    result.timeout_count,
                    result.error_count,
                    result.message,
                    result.route_action,
                    result.route_from,
                    result.route_to,
                    result.route_message,
                ),
            )
            cycle_id = int(connection.execute("SELECT last_insert_rowid()").fetchone()[0])
            cutoff = int(time.time()) - self.retention_days * 86400
            connection.execute("DELETE FROM samples WHERE sampled_at < ?", (cutoff,))
            connection.execute("DELETE FROM cycles WHERE finished_at < ?", (cutoff,))
            connection.execute("DELETE FROM route_events WHERE switched_at < ?", (cutoff,))
        return cycle_id

    def write_route_event(
        self,
        cycle_id: int,
        group_name: str,
        from_node: str,
        to_node: str,
        reason: str,
        switched_at: Optional[int] = None,
    ) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO route_events
                    (switched_at, cycle_id, group_name, from_node, to_node, reason)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    int(switched_at or time.time()),
                    cycle_id,
                    group_name,
                    from_node,
                    to_node,
                    reason,
                ),
            )

    def last_route_event(self) -> Optional[Dict[str, Any]]:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM route_events ORDER BY id DESC LIMIT 1"
            ).fetchone()
        return self._row_dict(row)

    def latest_results(self) -> List[Dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT s.sampled_at, s.local_date, s.node, s.status,
                       s.delay_ms, s.error, s.request_ms
                FROM samples AS s
                INNER JOIN (
                    SELECT node, MAX(id) AS max_id
                    FROM samples
                    GROUP BY node
                ) AS latest ON latest.max_id = s.id
                ORDER BY s.node COLLATE NOCASE
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def last_cycle(self) -> Optional[Dict[str, Any]]:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM cycles ORDER BY id DESC LIMIT 1"
            ).fetchone()
        return self._row_dict(row)

    @staticmethod
    def _day_bounds(target: date) -> Tuple[int, int]:
        start = datetime.combine(target, datetime_time.min).timestamp()
        end = datetime.combine(target + timedelta(days=1), datetime_time.min).timestamp()
        return int(start), int(end)

    def samples_for_day(self, target: date) -> List[Dict[str, Any]]:
        start, end = self._day_bounds(target)
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT sampled_at, local_date, node, status, delay_ms, error, request_ms
                FROM samples
                WHERE sampled_at >= ? AND sampled_at < ?
                ORDER BY sampled_at, node COLLATE NOCASE
                """,
                (start, end),
            ).fetchall()
        return [dict(row) for row in rows]

    def samples_for_range(self, start_date: date, end_date: date) -> List[Dict[str, Any]]:
        """Return raw samples from start_date through end_date, inclusive."""

        start, _ = self._day_bounds(start_date)
        _, end = self._day_bounds(end_date)
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT sampled_at, local_date, node, status, delay_ms, error, request_ms
                FROM samples
                WHERE sampled_at >= ? AND sampled_at < ?
                ORDER BY sampled_at, node COLLATE NOCASE
                """,
                (start, end),
            ).fetchall()
        return [dict(row) for row in rows]


class MonitorService:
    def __init__(self, settings: Settings, callback: Optional[Callable[[CycleResult], None]] = None) -> None:
        self.settings = settings
        self.callback = callback
        self.client = ClashClient(settings)
        self.store = Store(settings.database, settings.retention_days)
        self.executor = ThreadPoolExecutor(max_workers=settings.workers, thread_name_prefix="node-check")
        self.stop_event = threading.Event()
        self.wake_event = threading.Event()
        self._state_lock = threading.Lock()
        self._paused = False
        self._in_cycle = False
        self._cycle_started_at: Optional[int] = None
        self._cycle_completed = 0
        self._cycle_total = 0
        self._manual_refresh_requested = False
        self._next_due = 0.0
        self._last_nodes: List[str] = []
        self._route_bad_streak = 0
        self._last_route_at: Optional[int] = None
        self._current_route_node = ""
        self._current_route_control_group = ""
        self._current_route_path: Tuple[str, ...] = ()
        self._last_route_message = ""
        self._route_switching = False
        self._route_status_lock = threading.Lock()
        self._route_status_due = 0.0
        self.thread: Optional[threading.Thread] = None

    @property
    def paused(self) -> bool:
        with self._state_lock:
            return self._paused

    @property
    def in_cycle(self) -> bool:
        with self._state_lock:
            return self._in_cycle

    @property
    def cycle_progress(self) -> Tuple[Optional[int], int, int]:
        with self._state_lock:
            return self._cycle_started_at, self._cycle_completed, self._cycle_total

    @property
    def last_nodes(self) -> List[str]:
        with self._state_lock:
            return list(self._last_nodes)

    def routing_snapshot(self) -> Dict[str, Any]:
        settings = self.settings_snapshot()
        with self._state_lock:
            control_group = self._current_route_control_group or settings.route_group
            return {
                "enabled": settings.auto_route,
                "group": control_group,
                "requestedGroup": settings.route_group,
                "currentNode": self._current_route_node,
                "path": list(self._current_route_path),
                "badStreak": self._route_bad_streak,
                "lastRouteAt": self._last_route_at,
                "lastMessage": self._last_route_message,
                "switching": self._route_switching,
            }

    def refresh_route_state(self, force: bool = False) -> None:
        """Read the active Clash route independently from a sampling cycle.

        The card polls status even while sampling is paused. Keep this read
        short and cached so the UI can prove which Clash node is active
        without turning status polling into another monitor loop.
        """

        settings = self.settings_snapshot()
        if not settings.route_group:
            return
        now = time.monotonic()
        with self._state_lock:
            if self._route_switching or (not force and now < self._route_status_due):
                return
        if not self._route_status_lock.acquire(blocking=False):
            return
        try:
            now = time.monotonic()
            with self._state_lock:
                if self._route_switching or (not force and now < self._route_status_due):
                    return
            try:
                context = self.client.get_route_context(settings.route_group, timeout_seconds=1.5)
            except (ClashApiError, OSError, TypeError, ValueError) as exc:
                with self._state_lock:
                    self._route_status_due = time.monotonic() + 5.0
                    self._current_route_node = ""
                    self._current_route_control_group = ""
                    self._current_route_path = ()
                    self._last_route_message = f"无法确认 Clash 实际当前节点：{_short_error(str(exc))}"
                return

            latest_settings = self.settings_snapshot()
            if latest_settings.route_group != settings.route_group or not latest_settings.auto_route:
                if latest_settings.route_group != settings.route_group:
                    return
            with self._state_lock:
                previous_node = self._current_route_node
                previous_control_group = self._current_route_control_group
                previous_path = self._current_route_path
                self._route_status_due = time.monotonic() + 5.0
                self._current_route_node = context.current_node
                self._current_route_control_group = context.control_group
                self._current_route_path = context.path
                route_changed_externally = (
                    previous_node
                    and (
                        previous_node != context.current_node
                        or previous_control_group != context.control_group
                        or previous_path != context.path
                    )
                )
                if route_changed_externally:
                    # A manual Clash change or URLTest update starts a new
                    # health streak. Never carry failures from the old node
                    # into a different effective route.
                    self._route_bad_streak = 0
                if route_changed_externally or self._last_route_message in {"", "自动路由已重新初始化"} or self._last_route_message.startswith("无法确认 Clash"):
                    if context.control_group != context.requested_group:
                        self._last_route_message = (
                            f"已与 Clash 同步：实际出站组 {context.control_group} 当前节点 "
                            f"{context.current_node}（监控组 {context.requested_group}）"
                        )
                    else:
                        self._last_route_message = f"已与 Clash 同步：当前节点 {context.current_node}"
        finally:
            self._route_status_lock.release()

    def settings_snapshot(self) -> Settings:
        with self._state_lock:
            return self.settings

    def next_refresh_seconds(self) -> Optional[int]:
        with self._state_lock:
            if self._paused:
                return None
            due = self._next_due
        if due <= 0:
            return 0
        return max(0, int(math.ceil(due - time.monotonic())))

    def start(self) -> None:
        if self.thread and self.thread.is_alive():
            return
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._loop, name="clash-node-monitor", daemon=True)
        self.thread.start()

    def _loop(self) -> None:
        due_at = 0.0
        while not self.stop_event.is_set():
            wait_seconds = max(0.0, due_at - time.monotonic())
            self.wake_event.wait(wait_seconds)
            self.wake_event.clear()
            if self.stop_event.is_set():
                break
            with self._state_lock:
                manual = self._manual_refresh_requested
                self._manual_refresh_requested = False
                paused = self._paused
                interval_seconds = self.settings.interval_seconds
            if paused and not manual:
                due_at = time.monotonic() + interval_seconds
                with self._state_lock:
                    self._next_due = due_at
                continue
            self.run_cycle()
            with self._state_lock:
                interval_seconds = self.settings.interval_seconds
            due_at = time.monotonic() + interval_seconds
            with self._state_lock:
                self._next_due = due_at

    @staticmethod
    def _route_is_green(measurement: Measurement, warning_delay_ms: int) -> bool:
        return (
            measurement.status == "ok"
            and measurement.delay_ms is not None
            and measurement.delay_ms <= warning_delay_ms
        )

    @staticmethod
    def _route_is_hard_failure(measurement: Measurement) -> bool:
        """Return whether a failed route needs immediate failover protection.

        A timeout or controller/test error means the active node is not
        usable. These hard failures bypass the soft latency confirmation
        threshold and are retained as a separate reason in the route
        explanation; orange latency still uses the configured threshold.
        """

        return measurement.status in {"timeout", "error"}

    @staticmethod
    def _append_result_message(result: CycleResult, message: str) -> None:
        if not message:
            return
        result.message = f"{result.message}；{message}" if result.message else message

    def _maybe_route(self, result: CycleResult) -> None:
        settings = self.settings_snapshot()
        result.route_action = "none"
        result.route_from = ""
        result.route_to = ""
        result.route_message = ""
        result.route_group = ""
        with self._state_lock:
            self._route_switching = False

        if not settings.auto_route:
            with self._state_lock:
                self._route_bad_streak = 0
                self._last_route_message = "自动路由未启用"
            return
        result.route_group = settings.route_group
        if not result.measurements:
            result.route_action = "unavailable"
            result.route_message = "本轮没有完整节点结果，暂不自动路由"
            self._append_result_message(result, result.route_message)
            with self._state_lock:
                self._last_route_message = result.route_message
            return

        try:
            context = self.client.get_route_context(settings.route_group)
            current_node = context.current_node
            result.route_group = context.control_group

            with self._state_lock:
                previous_node = self._current_route_node
                previous_control_group = self._current_route_control_group
                previous_path = self._current_route_path
                self._current_route_node = current_node
                self._current_route_control_group = context.control_group
                self._current_route_path = context.path
                self._route_status_due = time.monotonic() + 5.0
                route_changed = (
                    previous_node
                    and (
                        previous_node != current_node
                        or previous_control_group != context.control_group
                        or previous_path != context.path
                    )
                )
                if route_changed:
                    self._route_bad_streak = 0
            measurements_by_node = {item.node: item for item in result.measurements}
            current_measurement = measurements_by_node.get(current_node)
            if current_measurement is None:
                result.route_action = "unavailable"
                result.route_message = f"当前节点 {current_node} 不在监控清单，暂不自动切换"
                self._append_result_message(result, result.route_message)
                with self._state_lock:
                    self._route_bad_streak = 0
                    self._last_route_message = result.route_message
                return

            if self._route_is_green(current_measurement, settings.warning_delay_ms):
                result.route_action = "kept"
                result.route_from = current_node
                result.route_message = f"当前节点 {current_node} 仍为绿色，保持不变"
                with self._state_lock:
                    self._route_bad_streak = 0
                    self._last_route_message = result.route_message
                return

            with self._state_lock:
                self._route_bad_streak += 1
                bad_streak = self._route_bad_streak
                last_route_at = self._last_route_at
            result.route_from = current_node
            hard_failure = self._route_is_hard_failure(current_measurement)
            if not hard_failure and bad_streak < settings.route_after_failures:
                result.route_action = "waiting"
                result.route_message = (
                    f"当前节点 {current_node} 未达绿色标准，已连续异常 {bad_streak}/"
                    f"{settings.route_after_failures} 次，等待确认"
                )
                self._append_result_message(result, result.route_message)
                with self._state_lock:
                    self._last_route_message = result.route_message
                return

            cooldown_active = bool(
                last_route_at
                and int(time.time()) - last_route_at < settings.route_cooldown_seconds
            )

            allowed_members = set(context.allowed_members)
            if not allowed_members or current_node not in allowed_members:
                result.route_action = "unavailable"
                result.route_message = (
                    f"监控组“{settings.route_group}”与 Clash 实际节点 {current_node} 不一致，"
                    "为避免误切换而保持原路由"
                )
                self._append_result_message(result, result.route_message)
                with self._state_lock:
                    self._last_route_message = result.route_message
                return
            candidates = [
                item for item in result.measurements
                if self._route_is_green(item, settings.warning_delay_ms)
                and item.node != current_node
                and item.node in allowed_members
            ]
            if not candidates:
                result.route_action = "unavailable"
                result.route_message = "当前节点已变为橙色/断开，但没有可用的绿色候选，保持原路由"
                self._append_result_message(result, result.route_message)
                with self._state_lock:
                    self._last_route_message = result.route_message
                return

            candidate = min(
                candidates,
                key=lambda item: (int(item.delay_ms or 10**9), natural_node_key(item.node)),
            )

            # Do not overwrite a choice made in Clash while this cycle was
            # evaluating delays.  Re-read immediately before PUT so a manual
            # selection or a changed rule path wins over an old sample.
            latest_context = self.client.get_route_context(settings.route_group)
            if latest_context.control_group != context.control_group or latest_context.current_node != current_node:
                with self._state_lock:
                    self._current_route_node = latest_context.current_node
                    self._current_route_control_group = latest_context.control_group
                    self._current_route_path = latest_context.path
                    self._route_bad_streak = 0
                    self._last_route_message = (
                        f"检测到 Clash 已由外部改为 {latest_context.current_node}，本轮不覆盖"
                    )
                result.route_action = "waiting"
                result.route_message = self._last_route_message
                self._append_result_message(result, result.route_message)
                return

            with self._state_lock:
                self._route_switching = True
            try:
                self.client.set_proxy(context.control_group, candidate.node)
                verified = self.client.get_proxy(context.control_group)
                verified_node = str(verified.get("now") or "").strip()
                if verified_node != candidate.node:
                    raise ClashApiError(
                        f"Clash 实际出站组 {context.control_group} 未确认已切换到 {candidate.node}"
                        f"（当前仍为 {verified_node or '未知'}）"
                    )
                verified_context = self.client.get_route_context(settings.route_group)
                if verified_context.control_group != context.control_group or verified_context.current_node != candidate.node:
                    raise ClashApiError(
                        f"Clash 最终实际节点不是 {candidate.node}（当前为 {verified_context.current_node or '未知'}）"
                    )
            finally:
                with self._state_lock:
                    self._route_switching = False
            result.route_action = "switched"
            result.route_to = candidate.node
            failure_is_hard = self._route_is_hard_failure(current_measurement)
            failure_label = "断线/错误" if failure_is_hard else "橙色延迟"
            failure_phrase = "首次完整采样即检测到故障" if failure_is_hard else "连续异常"
            result.route_message = (
                f"当前节点 {current_node} {failure_phrase}（{failure_label}），已通过实际出站组 {context.control_group} "
                f"切换到绿色最低延迟节点 {candidate.node}（{candidate.delay_ms} ms）"
                f"{('；故障转移已跳过冷却' if cooldown_active else '')}"
            )
            self._append_result_message(result, result.route_message)
            with self._state_lock:
                self._route_bad_streak = 0
                self._last_route_at = int(time.time())
                self._current_route_node = verified_context.current_node
                self._current_route_control_group = verified_context.control_group
                self._current_route_path = verified_context.path
                self._last_route_message = result.route_message
        except ClashApiError as exc:
            result.route_action = "error"
            result.route_message = f"自动路由未执行：{_short_error(str(exc))}"
            self._append_result_message(result, result.route_message)
            with self._state_lock:
                self._last_route_message = result.route_message
        except Exception as exc:
            result.route_action = "error"
            result.route_message = f"自动路由未执行：{_short_error(str(exc))}"
            self._append_result_message(result, result.route_message)
            with self._state_lock:
                self._last_route_message = result.route_message

    def run_cycle(self) -> CycleResult:
        started_at = int(time.time())
        with self._state_lock:
            self._in_cycle = True
            self._cycle_started_at = started_at
            self._cycle_completed = 0
            self._cycle_total = 0
        discovered: List[str] = []
        measurements: List[Measurement] = []
        message = ""
        try:
            discovered = self.client.list_nodes()
            with self._state_lock:
                self._last_nodes = list(discovered)
                self._cycle_total = len(discovered)
            futures = {self.executor.submit(self.client.test_node, node): node for node in discovered}
            for future in as_completed(futures):
                try:
                    measurements.append(future.result())
                except Exception as exc:  # keep one bad worker from stopping the cycle
                    node = futures[future]
                    measurements.append(
                        Measurement(
                            node=node,
                            sampled_at=int(time.time()),
                            status="error",
                            error=_short_error(f"测试线程异常：{exc}"),
                        )
                    )
                with self._state_lock:
                    self._cycle_completed += 1
            measurements.sort(key=lambda item: natural_node_key(item.node))
            if measurements and not all(item.status == "ok" for item in measurements):
                message = f"{sum(item.status != 'ok' for item in measurements)} 个节点需要关注"
        except ClashApiError as exc:
            message = _short_error(str(exc))
        except Exception as exc:
            message = _short_error(f"监控异常：{exc}")
        finally:
            finished_at = int(time.time())
            result = CycleResult(
                started_at=started_at,
                finished_at=finished_at,
                discovered_nodes=discovered,
                measurements=measurements,
                message=message,
            )
            if result.measurements:
                self._maybe_route(result)
            try:
                cycle_id = self.store.write_cycle(result)
                if result.route_action == "switched":
                    self.store.write_route_event(
                        cycle_id=cycle_id,
                        group_name=result.route_group or self.settings_snapshot().route_group,
                        from_node=result.route_from,
                        to_node=result.route_to,
                        reason=result.route_message,
                        switched_at=result.finished_at,
                    )
            except Exception as exc:
                result.message = _short_error(f"写入本地数据库失败：{exc}")
            with self._state_lock:
                self._in_cycle = False
                self._cycle_started_at = None
                self._cycle_completed = 0
                self._cycle_total = 0
            if self.callback:
                try:
                    self.callback(result)
                except Exception:
                    pass
        return result

    def request_refresh(self) -> bool:
        """Queue one refresh; never overlap or queue a stale second cycle."""

        with self._state_lock:
            if self._in_cycle:
                return False
            self._manual_refresh_requested = True
        self.wake_event.set()
        return True

    def update_settings(self, settings: Settings) -> None:
        with self._state_lock:
            route_group_changed = self.settings.route_group != settings.route_group
            auto_route_changed = self.settings.auto_route != settings.auto_route
            self.settings = settings
            self.client.settings = settings
            self.store.retention_days = settings.retention_days
            self._route_bad_streak = 0
            self._last_route_message = "自动路由已重新初始化"
            self._route_status_due = 0.0
            if route_group_changed or auto_route_changed:
                # A new route group or a newly enabled route must not inherit
                # a cooldown timestamp from a previous control context.
                self._last_route_at = None
            if route_group_changed:
                self._current_route_node = ""
                self._current_route_control_group = ""
                self._current_route_path = ()
        self.wake_event.set()

    def set_paused(self, paused: bool) -> None:
        with self._state_lock:
            self._paused = paused
        if not paused:
            self.wake_event.set()

    def stop(self) -> None:
        self.stop_event.set()
        self.wake_event.set()
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=2.0)
        self.executor.shutdown(wait=False, cancel_futures=True)


def status_label(status: str, delay_ms: Optional[int], error: str = "") -> str:
    if status == "ok" and delay_ms is not None:
        return f"正常 · {delay_ms} ms"
    if status == "timeout":
        return "Timeout"
    if status == "error":
        return "失败" if not error else f"失败 · {error}"
    return "未采样"


def status_color(status: str) -> str:
    return {"ok": "#79d6a4", "timeout": "#ff7d7d", "error": "#ffae71"}.get(status, "#9098aa")


def short_datetime(timestamp: Optional[int]) -> str:
    if not timestamp:
        return "—"
    return datetime.fromtimestamp(timestamp).strftime("%H:%M:%S")


def _aggregate_day_samples(
    rows: Sequence[Mapping[str, Any]],
    start_timestamp: int,
    bucket_seconds: int = 300,
) -> Dict[str, List[Dict[str, Any]]]:
    """Reduce samples into selectable time buckets for the dashboard chart."""

    bucket_seconds = max(1, int(bucket_seconds))
    bucket_count = max(1, math.ceil(86400 / bucket_seconds))

    buckets: Dict[str, Dict[int, Dict[str, Any]]] = {}
    for row in rows:
        node = str(row["node"])
        index = max(0, min(bucket_count - 1, int((int(row["sampled_at"]) - start_timestamp) / bucket_seconds)))
        node_buckets = buckets.setdefault(node, {})
        bucket = node_buckets.setdefault(index, {"ok": [], "timeout": 0, "error": 0})
        if row["status"] == "ok" and row["delay_ms"] is not None:
            bucket["ok"].append(int(row["delay_ms"]))
        elif row["status"] == "timeout":
            bucket["timeout"] += 1
        else:
            bucket["error"] += 1

    result: Dict[str, List[Dict[str, Any]]] = {}
    for node, node_buckets in buckets.items():
        points: List[Dict[str, Any]] = []
        for index, bucket in sorted(node_buckets.items()):
            ok_values = bucket["ok"]
            failures = bucket["timeout"] + bucket["error"]
            if ok_values and not failures:
                status = "ok"
            elif ok_values:
                status = "degraded"
            elif bucket["timeout"]:
                status = "timeout"
            else:
                status = "error"
            points.append(
                {
                    "index": index,
                    "timestamp": start_timestamp + index * bucket_seconds + bucket_seconds // 2,
                    "status": status,
                    "delay_ms": int(statistics.mean(ok_values)) if ok_values else None,
                }
            )
        result[node] = points
    return result


def _settings_payload(settings: Settings) -> Dict[str, Any]:
    """Expose dashboard-safe settings; the controller secret never leaves the process."""

    return {
        "intervalSeconds": settings.interval_seconds,
        "timeoutMs": settings.timeout_ms,
        "testUrl": settings.test_url,
        "nodePattern": settings.node_pattern,
        "selectedNodes": list(settings.selected_nodes),
        "controller": settings.controller,
        "hasSecret": bool(settings.secret),
        "serverPort": settings.server_port,
        "autoRoute": settings.auto_route,
        "routeGroup": settings.route_group,
        "routeAfterFailures": settings.route_after_failures,
        "routeCooldownSeconds": settings.route_cooldown_seconds,
        "warningDelayMs": settings.warning_delay_ms,
    }


def _cycle_payload(cycle: Optional[Mapping[str, Any]]) -> Optional[Dict[str, Any]]:
    if not cycle:
        return None
    return {
        "id": int(cycle.get("id") or 0),
        "startedAt": int(cycle.get("started_at") or 0),
        "finishedAt": int(cycle.get("finished_at") or 0),
        "status": str(cycle.get("status") or "error"),
        "nodeCount": int(cycle.get("node_count") or 0),
        "okCount": int(cycle.get("ok_count") or 0),
        "timeoutCount": int(cycle.get("timeout_count") or 0),
        "errorCount": int(cycle.get("error_count") or 0),
        "message": str(cycle.get("message") or ""),
        "routeAction": str(cycle.get("route_action") or "none"),
        "routeFrom": str(cycle.get("route_from") or ""),
        "routeTo": str(cycle.get("route_to") or ""),
        "routeMessage": str(cycle.get("route_message") or ""),
    }


def _route_event_payload(event: Optional[Mapping[str, Any]]) -> Optional[Dict[str, Any]]:
    if not event:
        return None
    return {
        "id": int(event.get("id") or 0),
        "switchedAt": int(event.get("switched_at") or 0),
        "cycleId": int(event.get("cycle_id") or 0),
        "groupName": str(event.get("group_name") or ""),
        "fromNode": str(event.get("from_node") or ""),
        "toNode": str(event.get("to_node") or ""),
        "reason": str(event.get("reason") or ""),
    }


def _measurement_payload(row: Mapping[str, Any]) -> Dict[str, Any]:
    delay = row.get("delay_ms")
    return {
        "sampledAt": int(row.get("sampled_at") or 0),
        "localDate": str(row.get("local_date") or ""),
        "node": str(row.get("node") or ""),
        "status": str(row.get("status") or "error"),
        "delayMs": int(delay) if delay is not None else None,
        "error": str(row.get("error") or ""),
        "requestMs": int(row.get("request_ms") or 0),
    }


EXPORT_FIELDNAMES = (
    "sampled_at_epoch",
    "sampled_at_local",
    "local_date",
    "node",
    "status",
    "delay_ms",
    "request_ms",
    "error",
)


def _export_csv(rows: Sequence[Mapping[str, Any]]) -> str:
    """Serialize raw monitor samples into a stable, AI-friendly CSV."""

    output = io.StringIO(newline="")
    writer = csv.DictWriter(
        output,
        fieldnames=EXPORT_FIELDNAMES,
        extrasaction="ignore",
        lineterminator="\r\n",
    )
    writer.writeheader()
    for row in rows:
        sampled_at = int(row.get("sampled_at") or 0)
        writer.writerow(
            {
                "sampled_at_epoch": sampled_at,
                "sampled_at_local": (
                    datetime.fromtimestamp(sampled_at).astimezone().isoformat(timespec="seconds")
                    if sampled_at
                    else ""
                ),
                "local_date": str(row.get("local_date") or ""),
                "node": str(row.get("node") or ""),
                "status": str(row.get("status") or "error"),
                "delay_ms": row.get("delay_ms"),
                "request_ms": int(row.get("request_ms") or 0),
                "error": str(row.get("error") or ""),
            }
        )
    return output.getvalue()


def _valid_test_url(value: Any, fallback: str) -> str:
    candidate = str(value or "").strip()
    return candidate if candidate.startswith(("http://", "https://")) else fallback


def _valid_pattern(value: Any, fallback: str) -> str:
    candidate = str(value or "").strip()
    if not candidate or len(candidate) > 200:
        return fallback
    try:
        re.compile(candidate)
    except re.error:
        return fallback
    return candidate


def _normalise_selected_nodes(value: Any, fallback: Sequence[str]) -> Tuple[str, ...]:
    if value is None:
        return tuple(fallback)
    if not isinstance(value, list):
        return tuple(fallback)
    return tuple(
        dict.fromkeys(
            str(item).strip() for item in value
            if isinstance(item, str) and item.strip()
        )
    )[:128]


class MonitorRuntime:
    """Own the sampler and the small loopback API used by the local dashboard."""

    def __init__(self, settings: Settings, config_path: Path) -> None:
        self.config_path = config_path
        self.service = MonitorService(settings)
        self._config_lock = threading.Lock()

    def start(self) -> None:
        self.service.start()

    def stop(self) -> None:
        self.service.stop()

    def status_payload(self) -> Dict[str, Any]:
        self.service.refresh_route_state()
        settings = self.service.settings_snapshot()
        cycle_started_at, completed, total = self.service.cycle_progress
        latest = self.service.store.latest_results()
        cycle = self.service.store.last_cycle()
        return {
            "ok": True,
            "apiVersion": API_VERSION,
            "fetchedAt": int(time.time()),
            "settings": _settings_payload(settings),
            "monitor": {
                "running": bool(self.service.thread and self.service.thread.is_alive()),
                "paused": self.service.paused,
                "inCycle": self.service.in_cycle,
                "nextRefreshSeconds": self.service.next_refresh_seconds(),
                "cycleStartedAt": cycle_started_at,
                "cycleProgress": {"completed": completed, "total": total},
            },
            "routing": self.service.routing_snapshot(),
            "availableNodes": self.service.last_nodes,
            "latest": [_measurement_payload(row) for row in latest],
            "lastCycle": _cycle_payload(cycle),
            "lastRouteEvent": _route_event_payload(self.service.store.last_route_event()),
        }

    def config_payload(self, refresh_nodes: bool = False) -> Dict[str, Any]:
        settings = self.service.settings_snapshot()
        available = self.service.last_nodes
        if refresh_nodes:
            try:
                available = self.service.client.list_available_nodes()
            except ClashApiError as exc:
                return {
                    "ok": False,
                    "message": _short_error(str(exc)),
                    "settings": _settings_payload(settings),
                    "availableNodes": available,
                }
        return {
            "ok": True,
            "apiVersion": API_VERSION,
            "settings": _settings_payload(settings),
            "availableNodes": available,
        }

    def history_payload(self, target: date, bucket_seconds: int = 300) -> Dict[str, Any]:
        settings = self.service.settings_snapshot()
        bucket_seconds = max(1, int(bucket_seconds))
        start, end = Store._day_bounds(target)
        rows = self.service.store.samples_for_day(target)
        grouped = _aggregate_day_samples(rows, start, bucket_seconds)
        series: Dict[str, List[Dict[str, Any]]] = {}
        for node, points in grouped.items():
            series[node] = [
                {
                    "timestamp": int(point["timestamp"]),
                    "status": str(point["status"]),
                    "delayMs": int(point["delay_ms"]) if point["delay_ms"] is not None else None,
                }
                for point in points
            ]
        return {
            "ok": True,
            "apiVersion": API_VERSION,
            "date": target.isoformat(),
            "startAt": start,
            "endAt": end,
            "bucketSeconds": bucket_seconds,
            "intervalSeconds": settings.interval_seconds,
            "series": series,
        }

    def export_csv(self, days: int = 1) -> Tuple[str, int, date, date]:
        """Export raw samples for the selected chart window."""

        days = int(days)
        if days not in {1, 3, 7}:
            days = 1
        end_date = date.today()
        start_date = end_date - timedelta(days=days - 1)
        rows = self.service.store.samples_for_range(start_date, end_date)
        return _export_csv(rows), len(rows), start_date, end_date

    def request_refresh(self) -> bool:
        return self.service.request_refresh()

    def test_connection(self, patch: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
        """Probe Clash with temporary connection values without saving them."""

        current = self.service.settings_snapshot()
        values = dict(patch or {})
        controller = _normalise_controller(str(values.get("controller") or current.controller))
        secret = current.secret
        if str(values.get("secret") or "").strip():
            secret = str(values["secret"]).strip()
        candidate = replace(current, controller=controller, secret=secret)
        nodes = ClashClient(candidate).list_available_nodes()
        return {
            "ok": True,
            "controller": controller,
            "hasSecret": bool(secret),
            "nodeCount": len(nodes),
            "availableNodes": nodes,
            "message": f"已连接，发现 {len(nodes)} 个可用节点",
        }

    def set_paused(self, paused: bool) -> None:
        self.service.set_paused(paused)

    def update_settings(self, patch: Mapping[str, Any]) -> Dict[str, Any]:
        current = self.service.settings_snapshot()
        interval = _positive_int(
            patch.get("intervalSeconds"),
            current.interval_seconds,
            15,
            3600,
        )
        timeout_ms = _positive_int(
            patch.get("timeoutMs"),
            current.timeout_ms,
            1000,
            30000,
        )
        controller = _normalise_controller(str(patch.get("controller") or current.controller))
        secret = current.secret
        secret_changed = False
        if patch.get("clearSecret") is True:
            secret = ""
            secret_changed = True
        elif str(patch.get("secret") or "").strip():
            secret = str(patch["secret"]).strip()
            secret_changed = True
        route_group = str(patch.get("routeGroup") if "routeGroup" in patch else current.route_group).strip()[:120]
        auto_route = patch.get("autoRoute") if isinstance(patch.get("autoRoute"), bool) else current.auto_route
        if auto_route and not route_group:
            auto_route = False
        next_settings = replace(
            current,
            controller=controller,
            secret=secret,
            interval_seconds=interval,
            timeout_ms=timeout_ms,
            test_url=_valid_test_url(patch.get("testUrl"), current.test_url),
            node_pattern=_valid_pattern(patch.get("nodePattern"), current.node_pattern),
            selected_nodes=_normalise_selected_nodes(patch.get("selectedNodes"), current.selected_nodes),
            auto_route=auto_route,
            route_group=route_group,
            route_after_failures=_positive_int(
                patch.get("routeAfterFailures"),
                current.route_after_failures,
                1,
                5,
            ),
            route_cooldown_seconds=_positive_int(
                patch.get("routeCooldownSeconds"),
                current.route_cooldown_seconds,
                30,
                3600,
            ),
            warning_delay_ms=_positive_int(
                patch.get("warningDelayMs"),
                current.warning_delay_ms,
                100,
                10000,
            ),
        )
        with self._config_lock:
            self._persist_runtime_settings(next_settings, persist_secret=secret_changed)
        self.service.update_settings(next_settings)
        if next_settings.auto_route:
            # Refresh the controller-side selection immediately after enabling
            # routing so the card never presents a stale/empty route target.
            self.service.refresh_route_state(force=True)
        return self.config_payload(refresh_nodes=False)

    def _persist_runtime_settings(self, settings: Settings, persist_secret: bool = False) -> None:
        raw: Dict[str, Any] = {}
        if self.config_path.is_file():
            try:
                loaded = json.loads(self.config_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    raw = loaded
            except (OSError, json.JSONDecodeError):
                raw = {}
        raw.update(
            {
                "controller": settings.controller,
                "interval_seconds": settings.interval_seconds,
                "timeout_ms": settings.timeout_ms,
                "test_url": settings.test_url,
                "node_pattern": settings.node_pattern,
                "selected_nodes": list(settings.selected_nodes),
                "workers": settings.workers,
                "retention_days": settings.retention_days,
                "database": str(settings.database),
                "always_on_top": settings.always_on_top,
                "server_host": settings.server_host,
                "server_port": settings.server_port,
                "auto_route": settings.auto_route,
                "route_group": settings.route_group,
                "route_after_failures": settings.route_after_failures,
                "route_cooldown_seconds": settings.route_cooldown_seconds,
                "warning_delay_ms": settings.warning_delay_ms,
            }
        )
        if persist_secret:
            raw["secret"] = settings.secret
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.config_path.with_suffix(self.config_path.suffix + ".tmp")
        temporary.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(self.config_path)


class MonitorApiHandler(BaseHTTPRequestHandler):
    server: "MonitorApiServer"

    def log_message(self, _format: str, *_args: Any) -> None:
        return

    def _send_json(self, status_code: int, payload: Mapping[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(body)

    def _send_csv(self, body: str, filename: str, row_count: int) -> None:
        payload = body.encode("utf-8-sig")
        self.send_response(200)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("X-Export-Row-Count", str(row_count))
        self.end_headers()
        self.wfile.write(payload)

    def _send_static(self, request_path: str) -> bool:
        """Serve the bundled browser UI without exposing files outside ``web``."""

        relative = urllib.parse.unquote(request_path)
        if relative in {"", "/"}:
            relative = "/index.html"
        if not relative.startswith("/"):
            return False
        relative_path = Path(relative.lstrip("/"))
        if any(part in {"", ".", ".."} for part in relative_path.parts):
            return False
        root = WEB_ROOT.resolve()
        candidate = (root / relative_path).resolve()
        if candidate != root and root not in candidate.parents:
            return False
        if not candidate.is_file():
            return False
        try:
            body = candidate.read_bytes()
        except OSError:
            return False
        content_type = STATIC_MIME_TYPES.get(candidate.suffix.lower(), "application/octet-stream")
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)
        return True

    def _read_json(self) -> Dict[str, Any]:
        length = _positive_int(self.headers.get("Content-Length"), 0, 0, 1024 * 1024)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) else {}

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/health":
            self._send_json(200, {"ok": True, "apiVersion": API_VERSION})
            return
        if parsed.path == "/api/status":
            self._send_json(200, self.server.runtime.status_payload())
            return
        if parsed.path == "/api/config":
            query = urllib.parse.parse_qs(parsed.query)
            refresh_nodes = query.get("refresh", ["0"])[0] == "1"
            payload = self.server.runtime.config_payload(refresh_nodes=refresh_nodes)
            self._send_json(200 if payload.get("ok") else 502, payload)
            return
        if parsed.path == "/api/history":
            query = urllib.parse.parse_qs(parsed.query)
            raw_date = query.get("date", [date.today().isoformat()])[0]
            raw_bucket = query.get("bucketSeconds", query.get("bucket", ["300"]))[0]
            bucket_seconds = _positive_int(raw_bucket, 300, 15, 3600)
            try:
                target = date.fromisoformat(raw_date)
            except ValueError:
                self._send_json(400, {"ok": False, "message": "日期格式应为 YYYY-MM-DD"})
                return
            self._send_json(200, self.server.runtime.history_payload(target, bucket_seconds))
            return
        if parsed.path == "/api/export":
            query = urllib.parse.parse_qs(parsed.query)
            raw_days = query.get("days", ["1"])[0]
            days = _positive_int(raw_days, 1, 1, 7)
            body, row_count, start_date, end_date = self.server.runtime.export_csv(days)
            filename = f"clash-node-monitor-{start_date.strftime('%Y%m%d')}-{end_date.strftime('%Y%m%d')}.csv"
            self._send_csv(body, filename, row_count)
            return
        if not parsed.path.startswith("/api/") and self._send_static(parsed.path):
            return
        if not parsed.path.startswith("/api/"):
            self._send_json(404, {"ok": False, "message": "页面资源不存在"})
            return
        self._send_json(404, {"ok": False, "message": "Not Found"})

    def do_POST(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/refresh":
            accepted = self.server.runtime.request_refresh()
            self._send_json(
                200,
                {
                    "ok": True,
                    "accepted": accepted,
                    "state": "sampling" if self.server.runtime.service.in_cycle else "queued" if accepted else "sampling",
                    "message": "本轮采样尚未完成" if not accepted else "已请求立即刷新",
                },
            )
            return
        if parsed.path == "/api/pause":
            payload = self._read_json()
            paused = bool(payload.get("paused", True))
            self.server.runtime.set_paused(paused)
            self._send_json(200, {"ok": True, "paused": paused})
            return
        if parsed.path == "/api/settings":
            payload = self.server.runtime.update_settings(self._read_json())
            self._send_json(200, payload)
            return
        if parsed.path == "/api/connection-test":
            try:
                payload = self.server.runtime.test_connection(self._read_json())
            except ClashApiError as exc:
                self._send_json(502, {"ok": False, "message": _short_error(str(exc))})
                return
            self._send_json(200, payload)
            return
        if parsed.path == "/api/shutdown":
            self._send_json(202, {"ok": True, "message": "监控程序正在关闭"})
            threading.Thread(target=self.server.shutdown, name="monitor-api-shutdown", daemon=True).start()
            return
        self._send_json(404, {"ok": False, "message": "Not Found"})


class MonitorApiServer(ThreadingHTTPServer):
    # Windows permits several SO_REUSEADDR listeners on the same port. That
    # made multiple dashboard windows launch independent samplers and requests
    # land on a random stale process. The monitor must be one process per port.
    allow_reuse_address = False
    daemon_threads = True

    def __init__(self, address: Tuple[str, int], runtime: MonitorRuntime) -> None:
        self.runtime = runtime
        super().__init__(address, MonitorApiHandler)

    def server_bind(self) -> None:
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def run_server(settings: Settings, config_path: Path, port: Optional[int] = None, open_browser: bool = False) -> int:
    runtime = MonitorRuntime(settings, config_path)
    bind_port = port or settings.server_port
    server = MonitorApiServer((settings.server_host, bind_port), runtime)
    runtime.start()
    if open_browser:
        threading.Timer(
            0.8,
            lambda: webbrowser.open(f"http://{settings.server_host}:{bind_port}/"),
        ).start()
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        runtime.stop()
    return 0


class DashboardApp:
    BG = "#171920"
    PANEL = "#222530"
    PANEL_RAISED = "#282c38"
    PANEL_ACCENT = "#1e2d3d"
    BORDER = "#363b49"
    TEXT = "#eff2f7"
    MUTED = "#99a1b2"
    ACCENT = "#78b9ee"
    ACCENT_HOVER = "#9bcfff"
    OK = "#79d6a4"
    WARN = "#ffae71"
    ERROR = "#ff7d7d"

    def __init__(self, settings: Settings) -> None:
        try:
            import tkinter as tk
            from tkinter import font as tkfont
        except ImportError as exc:  # pragma: no cover - platform dependent
            raise RuntimeError("当前 Python 没有 Tkinter，无法显示仪表盘") from exc

        self.tk = tk
        self.tkfont = tkfont
        self.settings = settings
        self.root = tk.Tk()
        self.root.title("节点监控")
        self.root.geometry("620x500")
        self.root.minsize(560, 430)
        self.root.configure(bg=self.BG)
        self.root.attributes("-topmost", settings.always_on_top)
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.bind("<Escape>", lambda _event: self.close())

        families = set(tkfont.families(self.root))
        self.font_family = next(
            (item for item in ("Microsoft YaHei UI", "Segoe UI", "Noto Sans CJK SC") if item in families),
            "TkDefaultFont",
        )
        self.mono_family = next(
            (item for item in ("Cascadia Mono", "Consolas", "Courier New") if item in families),
            self.font_family,
        )
        self.events: "queue.Queue[CycleResult]" = queue.Queue()
        self.service = MonitorService(settings, callback=self.events.put)
        self.node_order: List[str] = []
        self.node_widgets: Dict[str, Tuple[Any, Any]] = {}
        self.chart_window: Optional[Any] = None
        self.chart_canvas: Optional[Any] = None
        self.chart_date = date.today()
        self.chart_redraw_pending = False
        self._build_ui()
        self.root.after(200, self._refresh_ui)
        self.service.start()

    def font(self, size: int, weight: str = "normal", mono: bool = False) -> Tuple[str, int, str]:
        return (self.mono_family if mono else self.font_family, size, weight)

    def label(self, parent: Any, text: str = "", **kwargs: Any) -> Any:
        return self.tk.Label(parent, text=text, bg=kwargs.pop("bg", self.BG), fg=kwargs.pop("fg", self.TEXT), **kwargs)

    def button(self, parent: Any, text: str, command: Callable[[], None], accent: bool = False) -> Any:
        bg = self.ACCENT if accent else self.PANEL_RAISED
        fg = "#15202c" if accent else self.TEXT
        active_bg = self.ACCENT_HOVER if accent else "#353b4a"
        return self.tk.Button(
            parent,
            text=text,
            command=command,
            bg=bg,
            fg=fg,
            activebackground=active_bg,
            activeforeground=fg,
            relief="flat",
            bd=0,
            padx=12,
            pady=7,
            font=self.font(10, "bold"),
            cursor="hand2",
            takefocus=True,
            highlightthickness=1,
            highlightbackground=self.BORDER,
            highlightcolor=self.ACCENT,
        )

    def _build_ui(self) -> None:
        tk = self.tk
        outer = tk.Frame(self.root, bg=self.BG)
        outer.pack(fill="both", expand=True, padx=16, pady=14)

        header = tk.Frame(outer, bg=self.BG)
        header.pack(fill="x", pady=(0, 12))
        title_group = tk.Frame(header, bg=self.BG)
        title_group.pack(side="left")
        self.label(title_group, "节点监控", font=self.font(18, "bold")).pack(anchor="w")
        self.header_detail = self.label(
            title_group,
            f"Clash API · 每 {self.settings.interval_seconds} 秒采样",
            fg=self.MUTED,
            font=self.font(9),
        )
        self.header_detail.pack(anchor="w", pady=(2, 0))
        self.topmost_button = self.button(header, "已置顶", self.toggle_topmost)
        self.topmost_button.pack(side="right", padx=(8, 0))
        self.api_state = self.label(header, "连接中", fg=self.MUTED, font=self.font(10, "bold"))
        self.api_state.pack(side="right", pady=8)

        hero = tk.Frame(
            outer,
            bg=self.PANEL_ACCENT,
            highlightbackground="#35506b",
            highlightthickness=1,
        )
        hero.pack(fill="x", pady=(0, 12))
        hero_left = tk.Frame(hero, bg=self.PANEL_ACCENT)
        hero_left.pack(side="left", padx=16, pady=14)
        self.hero_value = self.label(hero_left, "等待首次采样", bg=self.PANEL_ACCENT, font=self.font(26, "bold"))
        self.hero_value.pack(anchor="w")
        self.hero_caption = self.label(hero_left, "正在读取节点", bg=self.PANEL_ACCENT, fg=self.MUTED, font=self.font(10))
        self.hero_caption.pack(anchor="w", pady=(2, 0))
        self.hero_refresh = self.label(hero, "最近刷新 —", bg=self.PANEL_ACCENT, fg=self.MUTED, font=self.font(10))
        self.hero_refresh.pack(side="right", padx=16, pady=16, anchor="n")

        stats = tk.Frame(outer, bg=self.BG)
        stats.pack(fill="x", pady=(0, 12))
        self.stat_available = self._stat_block(stats, "可用率")
        self.stat_average = self._stat_block(stats, "平均延迟")
        self.stat_alerts = self._stat_block(stats, "异常节点")

        list_card = tk.Frame(outer, bg=self.PANEL, highlightbackground=self.BORDER, highlightthickness=1)
        list_card.pack(fill="both", expand=True, pady=(0, 12))
        list_header = tk.Frame(list_card, bg=self.PANEL)
        list_header.pack(fill="x", padx=12, pady=(10, 4))
        self.label(list_header, "最近一次采样", bg=self.PANEL, font=self.font(11, "bold")).pack(side="left")
        self.list_hint = self.label(list_header, "", bg=self.PANEL, fg=self.MUTED, font=self.font(9))
        self.list_hint.pack(side="right")
        self.nodes_frame = tk.Frame(list_card, bg=self.PANEL)
        self.nodes_frame.pack(fill="both", expand=True, padx=12, pady=(0, 10))

        actions = tk.Frame(outer, bg=self.BG)
        actions.pack(fill="x")
        self.button(actions, "查看 24h 图", self.open_chart, accent=True).pack(side="left")
        self.button(actions, "立即刷新", self.refresh_now).pack(side="left", padx=(8, 0))
        self.pause_button = self.button(actions, "暂停", self.toggle_pause)
        self.pause_button.pack(side="left", padx=(8, 0))
        self.footer = self.label(actions, "启动后会立即进行第一次采样", fg=self.MUTED, font=self.font(9))
        self.footer.pack(side="right", pady=9)

    def _stat_block(self, parent: Any, title: str) -> Tuple[Any, Any]:
        frame = self.tk.Frame(parent, bg=self.PANEL, highlightbackground=self.BORDER, highlightthickness=1)
        frame.pack(side="left", fill="both", expand=True, padx=(0, 8))
        if title == "异常节点":
            frame.pack_configure(padx=(0, 0))
        self.label(frame, title, bg=self.PANEL, fg=self.MUTED, font=self.font(9)).pack(anchor="w", padx=12, pady=(9, 0))
        value = self.label(frame, "—", bg=self.PANEL, font=self.font(17, "bold", mono=True))
        value.pack(anchor="w", padx=12, pady=(2, 9))
        return frame, value

    def _ensure_node_widgets(self, names: Sequence[str]) -> None:
        names = list(names)
        if names == self.node_order:
            return
        for child in self.nodes_frame.winfo_children():
            child.destroy()
        self.node_widgets = {}
        self.node_order = names
        if not names:
            self.label(
                self.nodes_frame,
                "等待第一次采样；如果持续没有结果，请检查 Clash 是否运行以及控制器密钥。",
                bg=self.PANEL,
                fg=self.MUTED,
                font=self.font(9),
            ).pack(anchor="w", pady=8)
            return
        columns = 2 if len(names) > 5 else 1
        for index, name in enumerate(names):
            column = index // math.ceil(len(names) / columns)
            row = index % math.ceil(len(names) / columns)
            cell = self.tk.Frame(self.nodes_frame, bg=self.PANEL)
            cell.grid(row=row, column=column, sticky="ew", padx=(0, 14 if column == 0 and columns == 2 else 0), pady=2)
            self.nodes_frame.grid_columnconfigure(column, weight=1)
            name_label = self.label(cell, name, bg=self.PANEL, font=self.font(10, "bold", mono=True))
            name_label.pack(side="left")
            value_label = self.label(cell, "未采样", bg=self.PANEL, fg=self.MUTED, font=self.font(9))
            value_label.pack(side="right")
            self.node_widgets[name] = (name_label, value_label)

    def _refresh_ui(self) -> None:
        while True:
            try:
                self.events.get_nowait()
            except queue.Empty:
                break
        latest = self.service.store.latest_results()
        cycle = self.service.store.last_cycle()
        names = [str(row["node"]) for row in latest] or self.service.last_nodes
        self._ensure_node_widgets(sorted(set(names), key=natural_node_key))
        by_node = {str(row["node"]): row for row in latest}
        for name, (_name_label, value_label) in self.node_widgets.items():
            row = by_node.get(name)
            if row:
                value_label.configure(
                    text=status_label(str(row["status"]), row["delay_ms"], str(row.get("error") or "")),
                    fg=status_color(str(row["status"])),
                )
            else:
                value_label.configure(text="未采样", fg=self.MUTED)

        if latest:
            ok_rows = [row for row in latest if row["status"] == "ok"]
            delays = [int(row["delay_ms"]) for row in ok_rows if row["delay_ms"] is not None]
            timeout_count = sum(row["status"] == "timeout" for row in latest)
            error_count = sum(row["status"] == "error" for row in latest)
            total = len(latest)
            self.hero_value.configure(text=f"{len(ok_rows)} / {total}")
            self.hero_caption.configure(text="当前可用节点")
            self.stat_available[1].configure(text=f"{len(ok_rows) / total * 100:.0f}%")
            self.stat_average[1].configure(text=f"{round(statistics.mean(delays))} ms" if delays else "—")
            self.stat_alerts[1].configure(text=f"{timeout_count} 超时 · {error_count} 失败")
            latest_time = max(int(row["sampled_at"]) for row in latest)
            self.hero_refresh.configure(text=f"最近刷新 {short_datetime(latest_time)}")
            self.list_hint.configure(text=f"{total} 个节点")
        else:
            self.hero_value.configure(text="等待首次采样")
            self.hero_caption.configure(text="正在读取节点")
            self.stat_available[1].configure(text="—")
            self.stat_average[1].configure(text="—")
            self.stat_alerts[1].configure(text="—")
            self.hero_refresh.configure(text="最近刷新 —")
            self.list_hint.configure(text="")

        if self.service.in_cycle:
            self.api_state.configure(text="采样中", fg=self.ACCENT)
            self.footer.configure(text="正在测试节点…")
        elif cycle and cycle.get("status") == "error":
            self.api_state.configure(text="连接异常", fg=self.ERROR)
            self.footer.configure(text=_short_error(str(cycle.get("message") or "控制器不可用"), 90))
        elif self.service.paused:
            self.api_state.configure(text="已暂停", fg=self.WARN)
            self.footer.configure(text="监控已暂停")
        else:
            self.api_state.configure(text="监控中", fg=self.OK)
            remaining = self.service.next_refresh_seconds()
            self.footer.configure(text=f"下次刷新 {remaining}s" if remaining is not None else "等待刷新")

        self.pause_button.configure(text="继续" if self.service.paused else "暂停")
        self.root.after(1000, self._refresh_ui)

    def refresh_now(self) -> None:
        if not self.service.in_cycle:
            self.service.request_refresh()
            self.footer.configure(text="已请求立即刷新…")

    def toggle_pause(self) -> None:
        self.service.set_paused(not self.service.paused)

    def toggle_topmost(self) -> None:
        current = str(self.root.attributes("-topmost")).lower() in {"1", "true", "yes"}
        self.root.attributes("-topmost", not current)
        self.topmost_button.configure(text="已置顶" if not current else "未置顶")

    def open_chart(self) -> None:
        tk = self.tk
        if self.chart_window is not None and self.chart_window.winfo_exists():
            self.chart_window.deiconify()
            self.chart_window.lift()
            self._schedule_chart_redraw()
            return
        self.chart_window = tk.Toplevel(self.root)
        self.chart_window.title("节点监控 · 24 小时")
        self.chart_window.geometry("1080x680")
        self.chart_window.minsize(780, 520)
        self.chart_window.configure(bg=self.BG)
        self.chart_window.protocol("WM_DELETE_WINDOW", self._close_chart)
        self.chart_window.bind("<Escape>", lambda _event: self._close_chart())

        header = tk.Frame(self.chart_window, bg=self.BG)
        header.pack(fill="x", padx=16, pady=(14, 8))
        self.chart_title = self.label(header, "24 小时延迟线图", font=self.font(16, "bold"))
        self.chart_title.pack(side="left")
        self.chart_date_label = self.label(header, "", fg=self.MUTED, font=self.font(10, "bold", mono=True))
        self.chart_date_label.pack(side="left", padx=(12, 0), pady=3)
        self.button(header, "前一天", self.chart_previous).pack(side="right")
        self.button(header, "今天", self.chart_today).pack(side="right", padx=(8, 0))
        self.chart_next_button = self.button(header, "后一天", self.chart_next)
        self.chart_next_button.pack(side="right", padx=(8, 0))

        canvas_frame = tk.Frame(self.chart_window, bg=self.PANEL, highlightbackground=self.BORDER, highlightthickness=1)
        canvas_frame.pack(fill="both", expand=True, padx=16, pady=(0, 12))
        self.chart_canvas = tk.Canvas(canvas_frame, bg=self.PANEL, highlightthickness=0)
        self.chart_canvas.pack(fill="both", expand=True, padx=1, pady=1)
        self.chart_canvas.bind("<Configure>", lambda _event: self._schedule_chart_redraw())
        footer = self.label(
            self.chart_window,
            "蓝线：正常延迟  ·  橙色：同一 5 分钟内有成功和失败  ·  红色：Timeout/失败",
            fg=self.MUTED,
            font=self.font(9),
        )
        footer.pack(anchor="w", padx=18, pady=(0, 12))
        self._schedule_chart_redraw()

    def _close_chart(self) -> None:
        if self.chart_window is not None and self.chart_window.winfo_exists():
            self.chart_window.destroy()
        self.chart_window = None
        self.chart_canvas = None

    def _schedule_chart_redraw(self) -> None:
        if self.chart_redraw_pending:
            return
        self.chart_redraw_pending = True
        self.root.after_idle(self._draw_chart)

    def chart_previous(self) -> None:
        self.chart_date -= timedelta(days=1)
        self._schedule_chart_redraw()

    def chart_today(self) -> None:
        self.chart_date = date.today()
        self._schedule_chart_redraw()

    def chart_next(self) -> None:
        today = date.today()
        if self.chart_date < today:
            self.chart_date += timedelta(days=1)
            self._schedule_chart_redraw()

    def _draw_chart(self) -> None:
        self.chart_redraw_pending = False
        if self.chart_canvas is None or self.chart_window is None or not self.chart_window.winfo_exists():
            return
        canvas = self.chart_canvas
        canvas.delete("all")
        self.chart_date_label.configure(text=self.chart_date.strftime("%Y-%m-%d"))
        self.chart_next_button.configure(state="normal" if self.chart_date < date.today() else "disabled")

        width = max(780, canvas.winfo_width())
        height = max(460, canvas.winfo_height())
        left = 92
        right = width - 24
        top = 44
        bottom = height - 44
        day_start = int(datetime.combine(self.chart_date, datetime_time.min).timestamp())
        rows = self.service.store.samples_for_day(self.chart_date)
        grouped = _aggregate_day_samples(rows, day_start)
        nodes = sorted(grouped, key=natural_node_key)
        if not nodes:
            canvas.create_text(
                width / 2,
                height / 2 - 12,
                text="这一天还没有采样数据",
                fill=self.TEXT,
                font=self.font(16, "bold"),
            )
            canvas.create_text(
                width / 2,
                height / 2 + 18,
                text="启动监控并完成第一次采样后，趋势会出现在这里",
                fill=self.MUTED,
                font=self.font(10),
            )
            return

        delay_values = [
            int(row["delay_ms"])
            for row in rows
            if row["status"] == "ok" and row["delay_ms"] is not None
        ]
        max_delay = max(delay_values, default=500)
        max_delay = max(500, int(math.ceil(max_delay / 100.0) * 100))
        if max_delay > 10000:
            max_delay = int(math.ceil(max_delay / 1000.0) * 1000)

        lane_height = max(28.0, (bottom - top) / len(nodes))
        chart_width = right - left

        for hour in (0, 6, 12, 18, 24):
            x = left + chart_width * hour / 24
            canvas.create_line(x, top - 10, x, bottom, fill=self.BORDER if hour in (0, 24) else "#2b2f3b")
            anchor = "e" if hour == 24 else "w"
            canvas.create_text(x + (0 if hour not in (0, 24) else (-4 if hour == 0 else 4)), bottom + 16, text=f"{hour:02d}:00", anchor=anchor, fill=self.MUTED, font=self.font(9, mono=True))
        canvas.create_text(left - 8, top - 18, text=f"{max_delay} ms", anchor="e", fill=self.MUTED, font=self.font(8, mono=True))
        canvas.create_text(left - 8, bottom + 16, text="0 ms", anchor="e", fill=self.MUTED, font=self.font(8, mono=True))

        for index, node in enumerate(nodes):
            lane_top = top + index * lane_height
            lane_bottom = top + (index + 1) * lane_height
            inner_top = lane_top + 6
            inner_bottom = lane_bottom - 7
            canvas.create_line(left, inner_bottom, right, inner_bottom, fill="#303441")
            canvas.create_text(left - 10, (lane_top + lane_bottom) / 2, text=node, anchor="e", fill=self.TEXT, font=self.font(9, mono=True))
            previous: Optional[Tuple[int, float, float]] = None
            for point in grouped[node]:
                x = left + chart_width * ((point["timestamp"] - day_start) / 86400)
                status = point["status"]
                if status in {"timeout", "error"}:
                    bucket_left = left + chart_width * (point["index"] * 300 / 86400)
                    bucket_right = left + chart_width * ((point["index"] + 1) * 300 / 86400)
                    fill = "#503038" if status == "timeout" else "#503d32"
                    canvas.create_rectangle(bucket_left, inner_top, bucket_right, inner_bottom, fill=fill, outline="")
                    previous = None
                    continue
                delay = int(point["delay_ms"] or 0)
                ratio = min(1.0, max(0.0, delay / max_delay))
                y = inner_bottom - ratio * max(8.0, inner_bottom - inner_top)
                if previous and point["index"] - previous[0] <= 1:
                    canvas.create_line(previous[1], previous[2], x, y, fill=self.ACCENT if status == "ok" else self.WARN, width=1)
                color = self.ACCENT if status == "ok" else self.WARN
                radius = 2.2 if status == "ok" else 3.2
                canvas.create_oval(x - radius, y - radius, x + radius, y + radius, fill=color, outline="")
                previous = (point["index"], x, y)

        legend_y = max(18, top - 24)
        canvas.create_oval(left, legend_y - 3, left + 6, legend_y + 3, fill=self.ACCENT, outline="")
        canvas.create_text(left + 12, legend_y, text="正常延迟", anchor="w", fill=self.MUTED, font=self.font(8))
        canvas.create_oval(left + 76, legend_y - 3, left + 82, legend_y + 3, fill=self.WARN, outline="")
        canvas.create_text(left + 88, legend_y, text="降级", anchor="w", fill=self.MUTED, font=self.font(8))
        canvas.create_oval(left + 134, legend_y - 3, left + 140, legend_y + 3, fill=self.ERROR, outline="")
        canvas.create_text(left + 146, legend_y, text="Timeout/失败", anchor="w", fill=self.MUTED, font=self.font(8))

    def close(self) -> None:
        self.service.stop()
        self.root.destroy()


def _request_local_json(url: str, timeout_seconds: float = 1.2) -> Optional[Mapping[str, Any]]:
    request = urllib.request.Request(url, headers={"Accept": "application/json"}, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return payload if isinstance(payload, Mapping) else None
    except (OSError, TimeoutError, urllib.error.URLError, UnicodeDecodeError, json.JSONDecodeError):
        return None


def _floating_route_snapshot(payload: Optional[Mapping[str, Any]]) -> Tuple[str, str]:
    """Return the controller-confirmed node and its latest measured delay."""

    if not isinstance(payload, Mapping) or payload.get("ok") is not True:
        return "暂未读到", "服务未连接"
    routing = payload.get("routing")
    current = str(routing.get("currentNode") or "").strip() if isinstance(routing, Mapping) else ""
    current = current or "暂未读到"
    latest = payload.get("latest")
    if not isinstance(latest, list):
        return current, "最近一次采样未返回"
    measurement = next(
        (item for item in latest if isinstance(item, Mapping) and str(item.get("node") or "").strip() == current),
        None,
    )
    if not measurement:
        return current, "最近一次采样未返回"
    delay = measurement.get("delayMs")
    if isinstance(delay, (int, float)) and math.isfinite(float(delay)):
        return current, f"{round(float(delay))} ms"
    return current, "Timeout"


def _floating_interval_label(seconds: Any) -> str:
    try:
        value = max(1, int(seconds))
    except (TypeError, ValueError):
        value = 60
    if value < 60:
        return f"{value} 秒"
    if value % 60 == 0:
        return f"{value // 60} 分钟"
    return f"{value} 秒"


class FloatingLauncher:
    """A tiny draggable desktop handle; the dashboard remains the primary UI."""

    COLORS = {
        "background": "#12161e",
        "surface": "#171f26",
        "surface_raised": "#1d272f",
        "line": "#35414a",
        "ring": "#5c687d",
        "text": "#f7f9fc",
        "muted": "#aab6be",
        "accent": "#75c6c0",
        "ok": "#28b67a",
        "sampling": "#4c9cff",
        "warning": "#e4a33a",
        "offline": "#8b93a4",
        "error": "#db5c68",
    }
    SERIES_COLORS = ["#4c9cff", "#9b7bff", "#29b8a4", "#f29e74", "#d66f9d", "#7cbd55", "#e2b93b", "#62c7e8"]

    def __init__(self, settings: Settings) -> None:
        try:
            import tkinter as tk
        except ImportError as exc:  # pragma: no cover - platform dependent
            raise RuntimeError("当前 Python 没有 Tkinter，无法显示桌面入口") from exc

        self.tk = tk
        self.settings = settings
        self.root = tk.Tk()
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", settings.always_on_top)
        self.root.configure(bg="#010101")
        try:
            self.root.attributes("-transparentcolor", "#010101")
        except tk.TclError:
            pass
        self.root.geometry("56x56+96+96")
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.canvas = tk.Canvas(self.root, width=56, height=56, bg="#010101", highlightthickness=0)
        self.canvas.pack()
        self.canvas.bind("<ButtonPress-1>", self._begin_drag)
        self.canvas.bind("<B1-Motion>", self._drag)
        self.canvas.bind("<ButtonRelease-1>", self._end_drag)
        self.canvas.bind("<Button-3>", lambda _event: self.close())
        self.drag_origin: Optional[Tuple[int, int, int, int]] = None
        self.dragged = False
        self.online = False
        self.last_status: Optional[Mapping[str, Any]] = None
        self.tooltip_window: Optional[Any] = None
        self.tooltip_label: Optional[Any] = None
        self.chart_window: Optional[Any] = None
        self.chart_canvas: Optional[Any] = None
        self.chart_payload: Optional[Mapping[str, Any]] = None
        self.chart_hint: Optional[Any] = None
        self.chart_route_hint: Optional[Any] = None
        self.chart_footer: Optional[Any] = None
        self.canvas.bind("<Enter>", lambda _event: self._show_tooltip())
        self.canvas.bind("<Leave>", lambda _event: self._hide_tooltip())
        self._draw("offline")
        self.root.after(500, self._poll)

    @property
    def base_url(self) -> str:
        return f"http://{self.settings.server_host}:{self.settings.server_port}"

    def _draw(self, state: str) -> None:
        self.canvas.delete("all")
        color = self.COLORS.get(state, self.COLORS["offline"])
        self.canvas.create_oval(5, 5, 51, 51, fill=self.COLORS["background"], outline=self.COLORS["ring"], width=1)
        self.canvas.create_oval(9, 9, 47, 47, fill=color, outline="")
        self.canvas.create_text(28, 28, text="NM", fill=self.COLORS["text"], font=("Segoe UI", 9, "bold"))

    def _begin_drag(self, event: Any) -> None:
        self.drag_origin = (event.x_root, event.y_root, self.root.winfo_x(), self.root.winfo_y())
        self.dragged = False

    def _drag(self, event: Any) -> None:
        if not self.drag_origin:
            return
        start_x, start_y, window_x, window_y = self.drag_origin
        dx = event.x_root - start_x
        dy = event.y_root - start_y
        self.dragged = self.dragged or abs(dx) + abs(dy) > 3
        self.root.geometry(f"+{window_x + dx}+{window_y + dy}")

    def _end_drag(self, _event: Any) -> None:
        if not self.dragged:
            self.open_chart()
        self.drag_origin = None

    def _tooltip_text(self) -> str:
        current, delay = _floating_route_snapshot(self.last_status)
        payload = self.last_status or {}
        last_cycle = payload.get("lastCycle") if isinstance(payload, Mapping) else None
        finished_at = last_cycle.get("finishedAt") if isinstance(last_cycle, Mapping) else None
        return (
            f"节点监控\n"
            f"Clash 当前：{current}\n"
            f"当前延迟：{delay}\n"
            f"最近刷新：{short_datetime(int(finished_at)) if isinstance(finished_at, (int, float)) else '—'}\n"
            f"悬停看状态 · 单击打开趋势图 · 拖动移动 · 右键关闭"
        )

    def _show_tooltip(self) -> None:
        if self.tooltip_window is not None and self.tooltip_window.winfo_exists():
            self._update_tooltip()
            return
        tooltip = self.tk.Toplevel(self.root)
        tooltip.overrideredirect(True)
        tooltip.attributes("-topmost", True)
        tooltip.configure(bg=self.COLORS["line"])
        self.tooltip_window = tooltip
        self.tooltip_label = self.tk.Label(
            tooltip,
            text=self._tooltip_text(),
            justify="left",
            anchor="w",
            padx=10,
            pady=8,
            bg=self.COLORS["surface"],
            fg=self.COLORS["text"],
            font=("Segoe UI", 9),
        )
        self.tooltip_label.pack(padx=1, pady=1)
        self._update_tooltip()

    def _update_tooltip(self) -> None:
        if self.tooltip_window is None or not self.tooltip_window.winfo_exists():
            return
        if self.tooltip_label is not None and self.tooltip_label.winfo_exists():
            self.tooltip_label.configure(text=self._tooltip_text())
        self.tooltip_window.geometry(f"+{self.root.winfo_rootx() + 64}+{self.root.winfo_rooty() + 4}")

    def _hide_tooltip(self) -> None:
        if self.tooltip_window is not None and self.tooltip_window.winfo_exists():
            self.tooltip_window.destroy()
        self.tooltip_window = None
        self.tooltip_label = None

    def _route_hint_text(self) -> str:
        current, delay = _floating_route_snapshot(self.last_status)
        return f"Clash 当前：{current} · {delay}"

    def _poll(self) -> None:
        payload = _request_local_json(f"{self.base_url}/api/status")
        self.last_status = payload
        monitor = payload.get("monitor") if payload else None
        if payload and payload.get("ok") and isinstance(monitor, Mapping):
            self.online = True
            state = "sampling" if monitor.get("inCycle") else "warning" if monitor.get("paused") else "ok"
            self._draw(state)
        else:
            self.online = False
            self._draw("offline")
        self._update_tooltip()
        if self.chart_route_hint is not None and self.chart_route_hint.winfo_exists():
            self.chart_route_hint.configure(text=self._route_hint_text())
        self.root.after(3000, self._poll)

    def open_chart(self) -> None:
        tk = self.tk
        if self.chart_window is not None and self.chart_window.winfo_exists():
            self.chart_window.deiconify()
            self.chart_window.lift()
            self._load_chart()
            return
        self.chart_window = tk.Toplevel(self.root)
        self.chart_window.title("节点监控 · 24 小时")
        self.chart_window.geometry("980x620")
        self.chart_window.minsize(720, 460)
        surface = self.COLORS["surface"]
        raised = self.COLORS["surface_raised"]
        line = self.COLORS["line"]
        muted = self.COLORS["muted"]
        self.chart_window.configure(bg=surface)
        self.chart_window.protocol("WM_DELETE_WINDOW", self._close_chart)
        self.chart_window.bind("<Escape>", lambda _event: self._close_chart())
        header = tk.Frame(self.chart_window, bg=surface)
        header.pack(fill="x", padx=18, pady=(16, 10))
        tk.Label(header, text="节点监控", bg=surface, fg=self.COLORS["text"], font=("Segoe UI", 16, "bold")).pack(side="left")
        self.chart_hint = tk.Label(header, text="最近 24 小时 · 每 1 分钟聚合", bg=surface, fg=muted, font=("Segoe UI", 9))
        self.chart_hint.pack(side="left", padx=(12, 0), pady=3)
        self.chart_route_hint = tk.Label(header, text=self._route_hint_text(), bg=surface, fg=self.COLORS["accent"], font=("Segoe UI", 9, "bold"))
        self.chart_route_hint.pack(side="left", padx=(16, 0), pady=3)
        tk.Button(header, text="关闭", command=self._close_chart, relief="flat", bg=raised, fg=self.COLORS["text"], activebackground=line, activeforeground=self.COLORS["text"], padx=10, pady=5).pack(side="right")
        frame = tk.Frame(self.chart_window, bg=raised, highlightbackground=line, highlightthickness=1)
        frame.pack(fill="both", expand=True, padx=18, pady=(0, 12))
        self.chart_canvas = tk.Canvas(frame, bg=raised, highlightthickness=0)
        self.chart_canvas.pack(fill="both", expand=True, padx=1, pady=1)
        self.chart_canvas.bind("<Configure>", lambda _event: self._draw_chart())
        tk.Label(
            self.chart_window,
            text="彩色实线 = 延迟 · 红色小点 = Timeout/失败 · 单击浮点打开 · 右键关闭浮点",
            bg=surface,
            fg=muted,
            font=("Segoe UI", 9),
        ).pack(anchor="w", padx=18, pady=(0, 14))
        self._load_chart()

    def _load_chart(self) -> None:
        self.chart_payload = _request_local_json(f"{self.base_url}/api/history?date={date.today().isoformat()}&bucketSeconds=60")
        if self.chart_hint is not None and self.chart_hint.winfo_exists():
            bucket = self.chart_payload.get("bucketSeconds", 60) if isinstance(self.chart_payload, Mapping) else 60
            self.chart_hint.configure(text=f"最近 24 小时 · 每 {_floating_interval_label(bucket)} 聚合")
        if self.chart_route_hint is not None and self.chart_route_hint.winfo_exists():
            self.chart_route_hint.configure(text=self._route_hint_text())
        self._draw_chart()

    def _close_chart(self) -> None:
        if self.chart_window is not None and self.chart_window.winfo_exists():
            self.chart_window.destroy()
        self.chart_window = None
        self.chart_canvas = None
        self.chart_payload = None
        self.chart_hint = None
        self.chart_route_hint = None

    def _draw_chart(self) -> None:
        if self.chart_canvas is None or self.chart_window is None or not self.chart_window.winfo_exists():
            return
        canvas = self.chart_canvas
        canvas.delete("all")
        width = max(720, canvas.winfo_width())
        height = max(420, canvas.winfo_height())
        left, right, top, bottom = 82, width - 24, 38, height - 44
        payload = self.chart_payload or {}
        series = payload.get("series") if isinstance(payload, Mapping) else None
        if not isinstance(series, Mapping) or not series:
            canvas.create_text(width / 2, height / 2, text="今天还没有可显示的采样", fill=self.COLORS["text"], font=("Segoe UI", 14, "bold"))
            return
        start = int(payload.get("startAt") or int(datetime.combine(date.today(), datetime_time.min).timestamp()))
        end = int(payload.get("endAt") or start + 86400)
        values = [
            int(point.get("delayMs"))
            for points in series.values() if isinstance(points, list)
            for point in points if isinstance(point, Mapping) and point.get("delayMs") is not None
        ]
        max_delay = max(500, int(math.ceil(max(values, default=500) / 100.0) * 100))
        if max_delay > 10000:
            max_delay = int(math.ceil(max_delay / 1000.0) * 1000)
        chart_width = right - left
        chart_height = bottom - top
        for hour in (0, 6, 12, 18, 24):
            x = left + chart_width * hour / 24
            canvas.create_line(x, top, x, bottom, fill=self.COLORS["line"])
            canvas.create_text(x, bottom + 15, text=f"{hour:02d}:00", fill=self.COLORS["muted"], font=("Consolas", 8))
        canvas.create_text(left - 8, top, text=f"{max_delay} ms", anchor="e", fill=self.COLORS["muted"], font=("Consolas", 8))
        canvas.create_text(left - 8, bottom, text="0 ms", anchor="e", fill=self.COLORS["muted"], font=("Consolas", 8))
        nodes = sorted((str(name) for name in series.keys()), key=natural_node_key)
        for index, node in enumerate(nodes):
            points = series.get(node)
            if not isinstance(points, list):
                continue
            color = self.SERIES_COLORS[index % len(self.SERIES_COLORS)]
            previous: Optional[Tuple[float, float]] = None
            for point in points:
                if not isinstance(point, Mapping):
                    continue
                timestamp = int(point.get("timestamp") or start)
                x = left + chart_width * max(0.0, min(1.0, (timestamp - start) / max(1, end - start)))
                delay = point.get("delayMs")
                if delay is None:
                    canvas.create_oval(x - 2, bottom - 2, x + 2, bottom + 2, fill=self.COLORS["error"], outline="")
                    previous = None
                    continue
                y = bottom - min(1.0, max(0.0, int(delay) / max_delay)) * chart_height
                if previous is not None:
                    canvas.create_line(previous[0], previous[1], x, y, fill=color, width=2)
                canvas.create_oval(x - 2, y - 2, x + 2, y + 2, fill=color, outline="")
                previous = (x, y)
            legend_x = left + (index % 4) * 190
            legend_y = 12 + (index // 4) * 16
            canvas.create_oval(legend_x, legend_y - 3, legend_x + 7, legend_y + 4, fill=color, outline="")
            canvas.create_text(legend_x + 12, legend_y, text=node, anchor="w", fill=self.COLORS["text"], font=("Consolas", 8))

    def close(self) -> None:
        self._close_chart()
        self.root.destroy()


def print_cycle(result: CycleResult) -> None:
    timestamp = datetime.fromtimestamp(result.finished_at).strftime("%Y-%m-%d %H:%M:%S")
    print(
        f"[{timestamp}] 节点={len(result.discovered_nodes)} "
        f"正常={result.ok_count} 超时={result.timeout_count} 失败={result.error_count} "
        f"耗时={result.duration_ms}ms"
        + (f" · {result.message}" if result.message else "")
    )
    if result.route_message:
        print(f"  路由：{result.route_message}")
    for item in result.measurements:
        print(f"  {item.node}: {status_label(item.status, item.delay_ms, item.error)}")


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="低占用监控 Clash Verge 的叶子节点")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH, help="JSON 配置文件路径")
    parser.add_argument("--interval", type=int, help="覆盖采样间隔（秒），例如 30")
    parser.add_argument("--timeout", type=int, help="覆盖单节点超时（毫秒）")
    parser.add_argument("--once", action="store_true", help="只采样一次并退出")
    parser.add_argument("--no-ui", action="store_true", help="无界面常驻运行")
    parser.add_argument("--server", action="store_true", help=f"启动 loopback API 服务（默认端口 {DEFAULT_SERVER_PORT}）")
    parser.add_argument("--open-browser", action="store_true", help="启动服务后打开本机控制台")
    parser.add_argument("--port", type=int, help="覆盖 loopback API 端口")
    parser.add_argument("--floating", action="store_true", help="显示可拖动的桌面监控小圆点")
    parser.add_argument("--window", action="store_true", help="兼容模式：打开旧式完整桌面窗口")
    return parser


def run_headless(settings: Settings) -> int:
    service = MonitorService(settings, callback=print_cycle)
    service.start()
    print(f"Clash 节点监控已启动：{settings.controller}，间隔 {settings.interval_seconds}s；按 Ctrl+C 停止")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("正在停止…")
    finally:
        service.stop()
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_argument_parser()
    args = parser.parse_args(argv)
    settings = load_settings(args.config)
    if args.interval is not None:
        settings = replace(settings, interval_seconds=_positive_int(args.interval, settings.interval_seconds, 10, 86400))
    if args.timeout is not None:
        settings = replace(settings, timeout_ms=_positive_int(args.timeout, settings.timeout_ms, 1000, 30000))
    if args.port is not None:
        settings = replace(settings, server_port=_positive_int(args.port, settings.server_port, 1024, 65535))

    if args.once:
        service = MonitorService(settings)
        try:
            result = service.run_cycle()
            print_cycle(result)
            return 0 if result.measurements else 1
        finally:
            service.stop()

    if args.no_ui:
        return run_headless(settings)

    if args.floating:
        try:
            launcher = FloatingLauncher(settings)
            launcher.root.mainloop()
            return 0
        except RuntimeError as exc:
            print(str(exc), file=sys.stderr)
            return 2

    if args.server or not args.window:
        return run_server(
            settings,
            args.config,
            args.port,
            open_browser=args.open_browser or getattr(sys, "frozen", False),
        )

    try:
        app = DashboardApp(settings)
        app.root.mainloop()
        return 0
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
