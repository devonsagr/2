# Clash Node Monitor

Clash/mihomo 的本机节点质量监控工具。它读取 External Controller 的延迟结果，保留真实历史，提供可视化趋势和一键 CSV 导出；默认只在本机运行，不上传监控数据。

## 最简单的使用方式

1. 确认 Clash 或 mihomo 已启动，并开启 External Controller。
2. 从 GitHub 的 `release/ClashNodeMonitor.exe` 下载 EXE。
3. 双击 EXE。它会启动本机服务并自动打开控制台页面。
4. 页面先自动发现本机控制器；如果发现失败，打开“连接高级设置”，填写控制器地址和 API 密钥，再点击“测试连接”。
5. 首次使用默认监控所有可识别的叶子节点。需要缩小范围时，可以填写筛选规则或在“手动节点清单”中勾选节点。

EXE 是完整入口，不需要单独打开 `web/index.html`，也不需要安装 Node.js、数据库或浏览器插件。页面实际运行在本机 loopback 服务上，关闭服务即可停止监控。

## 从源码运行

需要 Windows、Python 3.10 或更高版本，以及正在运行的 Clash/mihomo External Controller：

```powershell
py clash_node_monitor.py --server --open-browser
```

浏览器打开 <http://127.0.0.1:17997/>。也可以双击 `run_monitor.bat`；它会优先使用 `release/ClashNodeMonitor.exe`，没有 EXE 时才回退到 Python。

其他入口：

```powershell
py clash_node_monitor.py --once       # 只做一次采样
py clash_node_monitor.py --no-ui      # 无界面常驻
py clash_node_monitor.py --floating   # 服务已运行时打开桌面浮点
```

## 如何连接 Clash

软件不会模拟点击 Clash 界面，而是使用 Clash/mihomo 的 External Controller API。启动后按以下顺序尝试：

- 读取本机常见配置中的 `external-controller` 和 `secret`。
- 使用页面中的“测试连接”验证控制器和节点清单。
- 自动发现失败时，在“连接高级设置”填写类似 `http://127.0.0.1:9097` 的控制器地址；有密钥就填写 API 密钥，没有密钥则留空。
- 点击“保存设置”后，地址和其它采样设置会保存在本机配置文件；密钥只在用户明确填写或清除时更新。

如果仍然连接不上，请检查 Clash/mihomo 是否启用了 External Controller、端口是否正确，以及控制器是否允许本机访问。软件不会替用户修改 Clash 配置。

## 趋势与 AI 分析

趋势图支持 1/3/7 天窗口和展示聚合。点击“导出 CSV”会导出当前时间范围内的原始采样，不会因为图表聚合而丢数据。文件包含：

- `sampled_at_local`：本地采样时间
- `node`：节点名称
- `status`：正常、超时或失败
- `delay_ms`：延迟（毫秒）
- `request_ms`：请求耗时（毫秒）
- `error`：错误摘要（如有）

导出的 UTF-8 CSV 可以直接交给 AI，适合分析可用率、超时分布、延迟趋势和节点之间的稳定性差异。

## 配置

复制 `monitor_config.example.json` 为同目录的 `monitor_config.json` 后按需修改。常用字段：

- `controller` / `secret`：External Controller 地址和 API 密钥
- `interval_seconds`：采样间隔，默认 60 秒
- `timeout_ms`：单节点超时，默认 5000 毫秒
- `node_pattern`：节点名称正则，默认 `.*`
- `selected_nodes`：可选，填写后只监控指定节点
- `test_url`：延迟探针地址
- `server_host` / `server_port`：本机 Web 服务地址，默认 `127.0.0.1:17997`
- `auto_route`：自动路由开关，默认关闭

也支持 `CLASH_SECRET` 和 `CLASH_CONFIG_PATH` 环境变量。SQLite 历史保存在 `data/node_monitor.sqlite3`，不会进入 Git。

## 构建 Windows EXE

```powershell
powershell -ExecutionPolicy Bypass -File .\build_exe.ps1
```

构建脚本会安装/使用 PyInstaller，把 `web/` 一起打入 `release/ClashNodeMonitor.exe`。发布前不要把 `monitor_config.json`、API 密钥或 `data/` 数据库放进仓库。

## 产品资料

- [公开名称与 About 文案建议](PUBLIC_PROFILE.md)
- [产品合同](PRODUCT.md)
- [设计合同](DESIGN.md)
- [架构说明](docs/架构说明.md)
- [需求与验收](docs/需求沉淀.md)
- [当前路线图](路线图.md)

## 验证

```powershell
py -m py_compile clash_node_monitor.py
py -m unittest discover -s tests -v
```
