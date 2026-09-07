# 节点监控

一个只使用 Python 标准库的 Clash Verge/mihomo 节点监控器。它直接调用 Clash External Controller 的延迟 API，不模拟点击、不依赖 AI 常驻，结果保存在本地 SQLite，并通过同一个 loopback 服务提供独立 Web 控制台。

## 快速开始

不要直接双击 `web/index.html`：它只是前端资源，直接打开时不会连接本机监控 API。请双击 `run_monitor.bat`，它会启动本机服务并打开独立控制台。当前本机的 Clash Verge 配置会自动发现 `127.0.0.1:9097` 和 API 密钥，通常无需填写密钥：

```powershell
py tw_monitor.py --server
```

脚本启动本机 `127.0.0.1:17997` loopback 服务并立即采样一次，之后默认每 60 秒采样。浏览器打开 <http://127.0.0.1:17997/> 即可使用独立控制台；采样不调用 AI。

这份目录可以脱离 Obsidian 使用。只需要 Windows、Python 3.10+ 和正在运行的 Clash Verge/mihomo External Controller；不需要 Node.js、前端构建工具或云端账号。

趋势图旁的“导出 CSV”会按当前选中的 1/3/7 天范围导出原始采样。文件包含 `sampled_at_local`、`node`、`status`、`delay_ms`、`request_ms` 和 `error` 等字段，适合直接交给 AI 做稳定性、超时分布和节点对比分析；图表聚合设置只影响展示，不会减少导出的原始行。

需要桌面浮点入口时，在服务已经运行的前提下另开：

```powershell
py tw_monitor.py --floating
```

浮点是可拖动的小圆点，点击打开当天趋势，右键关闭；它不会再启动第二套采样线程。`run_monitor.bat` 会用 `pythonw` 静默启动服务并打开浏览器控制台。

单次实测（适合先验收）：

```powershell
py tw_monitor.py --once
```

无界面常驻（兼容任务计划；Obsidian 卡片推荐使用 `--server`）：

```powershell
py tw_monitor.py --no-ui
```

若希望每 30 秒采样：

```powershell
py tw_monitor.py --interval 30
```

## 配置

复制 `monitor_config.example.json` 为同目录的 `monitor_config.json` 后按需修改。真实配置已加入 `.gitignore`，不要把 `secret` 提交到 Git。

常用项：

- `interval_seconds`：周期，默认 60；可设 30。
- `timeout_ms`：单节点 API 超时，默认 5000。
- `node_pattern`：节点名称正则，默认只匹配 `TW-1`～`TW-10`。
- `test_url`：延迟探针地址。
- `retention_days`：本地历史保留天数，默认 30。
- `selected_nodes`：可选；填写后优先于正则筛选。
- `server_host` / `server_port`：loopback API，默认 `127.0.0.1:17997`。
- `auto_route`：自动路由开关，默认 `false`；只有在用户显式开启后才允许调用 Clash 的 PUT 选择接口。
- `route_group`：Clash Selector/URLTest 路由组，默认 `TW自动选择`。
- `route_after_failures`：当前节点连续异常次数，默认 2。
- `route_cooldown_seconds`：成功切换后的冷却时间，默认 300 秒。
- `warning_delay_ms`：绿色延迟上限，默认 800ms；超过此值为橙色候选，不会被自动选中。

也支持 `CLASH_SECRET` 和 `CLASH_CONFIG_PATH` 环境变量覆盖自动发现结果。

## 数据与边界

数据文件为 `data/tw_monitor.sqlite3`，不会上传。默认只观察和记录，不会选择节点、修改 Clash 配置或自动切换网络。启用自动路由后也只在完整采样周期结束、当前节点连续异常且存在同组绿色候选时切换；绿色的第二名不会被替换。需求分层和验收标准见 [`docs/需求沉淀.md`](docs/需求沉淀.md)，独立 Web 结构和 API 边界见 [`docs/架构说明.md`](docs/架构说明.md)。

公开使用时只分发源码、`web/`、配置模板和启动脚本；不要分发本机的 `monitor_config.json`、API 密钥或 `data/tw_monitor.sqlite3`。

## 验证

```powershell
py -m py_compile tw_monitor.py
py -m unittest discover -s tests -v
```
