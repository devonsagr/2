# CODEX_HISTORY

## 2026-09-01：趋势图默认 30 分钟聚合

- 需求：后台仍按设置的 1 分钟间隔采样和写入 SQLite；趋势图默认改用 1800 秒（30 分钟）聚合，减少多日 SVG 点数和挂后台时的渲染压力；用户仍可手动选择 1 分钟、30 秒等细粒度查看。
- 实现：卡片默认 `chartBucket=1800`，30 分钟选项标注“默认”；选择 1 分钟仍请求 `bucketSeconds=60`，不改监控服务采样频率。
- 验证：节点卡定向测试 `9/9`、TypeScript、统一发布快照生产构建通过；正式 `main.js` SHA-256=`0F291F0C502135EDD7432379588770AA3CFA2C07875960C1AAB0C8156686A398`，`data.json` 未变；运行中的周期 `15572` 仍实际验证 `TW-10 Timeout → TW-6（86 ms）`，周期 `15577` 保持绿色节点。
- 发布：仪表盘提交 `3d1da92`、1 分钟细粒度回归测试提交 `8f2485a`、架构契约提交 `81ea5b1`；回滚备份：`D:\AAAcodex项目\仪表盘\.tmp\deploy-backups\jarvis-dashboard-before-chart-default-20260901-2245`。

## 2026-09-01：Timeout/错误首样本立即故障转移

- 根因：截图中的当前节点第一次出现 `Timeout` 时仍被 `route_after_failures=2` 的统一确认门槛拦住，导致卡片显示 `1/2`，即使已有绿色候选也不会在该轮切换；这不是 Clash PUT 失败。
- 实现：自动路由保持“绿色不动、橙色按可调确认次数”的策略；当前节点首次完整采样为 `timeout` 或 `error` 且存在绿色候选时立即执行故障转移。切换前重新读取实际 `MATCH` 出站 Selector，PUT 后 GET 回读控制节点和最终有效节点。
- 验证：新增首个 Timeout 即切换的回归测试；Python 单测 `17/17`、节点卡定向测试 `9/9`、TypeScript、统一发布快照生产构建通过。真实周期 `15414` 验证 `TW-7 Timeout → TW-6（82 ms）`，重启最终代码后的周期 `15431` 又验证 `TW-10 Timeout → TW-9（83 ms）`，实际 `主代理` 回读一致。
- 发布：仪表盘节点监控文案/测试提交 `da105d4`、架构契约提交 `9952144`；使用统一发布快照部署到 Obsidian，`main.js`/`styles.css` 与候选包 SHA-256 一致，安装器校验 46 个产物并保留 `data.json`。回滚备份：`D:\AAAcodex项目\仪表盘\.tmp\deploy-backups\jarvis-dashboard-before-tw-immediate-20260901-1953`。

## 2026-08-29：TW 节点监控首版

- 决策：使用 Clash/mihomo External Controller 的 `/proxies/{name}/delay`，不模拟鼠标点击；只读本地配置，不调用节点切换接口。
- 交付：标准库 Python 监控脚本、SQLite 明细、置顶仪表盘卡片、按天 24 小时图、无界面/单次模式、需求和架构文档。
- 范围：默认匹配 `^TW-\d+$`，每 60 秒一次，单节点 5 秒超时，保留 30 天。
- 验证：当前 Clash Verge 实测发现 10 个节点，4 个正常、6 个超时；单元测试 5/5；GUI 和图表冒烟通过。
- 风险：延迟 API 表示探针可达性/响应时间，不代表带宽；当前没有 Clash Verge 前端源码，因此卡片是独立本地窗口。
- 回滚：停止 `tw_monitor.py` 或 `run_monitor.bat` 即可停止监控；历史数据库为独立本地文件，不会改动 Clash 配置。
