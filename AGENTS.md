# 独立节点网络监控项目约定

## 项目映射

- 本地工程：`D:\AAAcodex项目\网络监控独立版`
- Git：未初始化；未获得目标 GitHub 仓库地址前不初始化、不上传
- 本地权威文档根：`docs/`，产品与设计合同为根目录 `PRODUCT.md`、`DESIGN.md`
- 当前路线图：`路线图.md`
- 当前交接：`当前交接.md`
- 需求保真记录：`docs/需求沉淀.md`
- 设计基线：`.ui-craft/brief.md`
- 视觉参考：`docs/视觉方向.md` 与 `docs/视觉参考/节点监控-参考图-v1.png`
- Obsidian 镜像：未声明，不同步

## 运行与验证

- loopback 服务（独立 Web 控制台）：`py tw_monitor.py --server`
- 桌面浮点（先确保服务已运行）：`py tw_monitor.py --floating`
- 兼容旧式完整 Tk 窗口：`py tw_monitor.py --window`
- 单次实测：`py tw_monitor.py --once`
- 无界面常驻：`py tw_monitor.py --no-ui`
- 单元测试：`py -m unittest discover -s tests -v`
- 语法检查：`py -m py_compile tw_monitor.py`

## 安全边界

- 只读取 Clash Verge 的本地控制器配置和 API，不上传数据；自动路由默认关闭，只有用户在卡片中显式开启且满足连续异常、绿色候选和冷却条件时才调用 Clash 选择接口。
- API 密钥不写入仓库；优先从 Clash 配置或 `CLASH_SECRET` 环境变量读取，也可在本地未跟踪的 `monitor_config.json` 中配置。
- 监控数据只落在本地 `data/tw_monitor.sqlite3`，该文件不提交 Git。
- `web/` 由 Python loopback 服务同源提供，不依赖 Node.js、打包器或第三方 CDN。
- 发布包不得包含真实 `monitor_config.json`、API 密钥、SQLite 数据库或缓存目录。
- 默认只匹配 `TW-1`～`TW-10` 这类叶子节点，排除 URLTest、LoadBalance 等策略组。
- 默认路由组为 `TW自动选择`；当前节点绿色时即使不是延迟第一名也保持不变，路由动作只写入本地 `route_events`。
