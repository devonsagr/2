# Product

<!-- impeccable:product-schema 1 -->

## Product name

工作名称：Clash Node Monitor（中文显示名：Clash 节点监控）。公开名称和 GitHub About 的可复制版本见 [`PUBLIC_PROFILE.md`](PUBLIC_PROFILE.md)。

## Platform and stack

Windows 本地软件。Python 标准库负责采样、SQLite、loopback API 和可选桌面入口；原生 HTML/CSS/JavaScript 负责控制台；PyInstaller 负责可选 EXE 打包。运行时不依赖 Node.js、第三方 CDN、账号或云服务。

## Users

长期使用 Clash/mihomo、希望快速判断节点可用性和延迟稳定性的个人用户。用户不应为了查看状态而维护复杂服务，也不应被迫理解前端构建流程。

## Product purpose

节点监控通过 Clash/mihomo External Controller 定时探测用户配置的叶子节点，把最近一次完整采样、可用率、平均延迟、异常状态和 1/3/7 天历史集中到独立控制台。成功标准是用户启动后能在 10 秒内判断当前可用情况，在 30 秒内定位持续不稳定的节点。

## Positioning

它不是 Clash 的通用控制面板，也不是带宽测速器；它是一个本机优先、可解释、默认只读的节点质量观察器。延迟探针说明响应延迟和可达性，不代表下载速度或完整业务体验。

## Operating context

- 用户在本机运行 Clash/mihomo，并启用 External Controller。
- 软件优先自动发现本机控制器；发现失败时提供可见的地址、密钥和连接测试入口。
- 服务默认只绑定 `127.0.0.1`，数据只写入本机 SQLite。
- 默认匹配所有可识别的叶子节点，策略组、URLTest 和 LoadBalance 不作为独立节点采样；用户可以用正则或手动清单缩小范围。
- 用户可查看 1/3/7 天历史、切换节点聚焦和展示聚合，并导出当前窗口的原始 CSV。

## Capabilities and constraints

- Python 核心负责 Clash API、有限并发采样、完整周期提交、SQLite、loopback JSON API、路由判断和可选桌面浮点入口。
- 自动路由默认关闭；用户显式开启后，只有在连续异常、绿色候选和实际控制器状态满足条件时才调用选择接口，并回读最终节点。
- 控制器暂时不可用时，界面显示离线/错误和上一次完整结果，不伪造在线数值。
- 导出只生成本机下载文件，包含采样时间、节点、状态、延迟、请求耗时和错误摘要。
- 公开发布不得包含真实配置、密钥、SQLite 数据、缓存或私人路径。

## Product principles

1. 先判断现在能不能用，再追查为什么。
2. 历史是真实采样记录，不用装饰性数据填空。
3. 自动路由只有在用户明确授权和规则满足时才改变网络。
4. 本机数据留在本机，服务默认最小暴露面。
5. 第一次使用者不需要编辑源码或手动拼接网页地址。

## Accessibility

控制台支持键盘操作、可见焦点、文字状态和 `aria-live` 更新；图表提供可读标题、点位明细和非颜色状态线索；浅色和深色主题都保持可读对比；`prefers-reduced-motion` 时关闭非必要动效。

## Out of scope

- 修改或注入 Clash/mihomo 的原始界面。
- 云端账号、远程监控、遥测和软件内 AI 常驻分析。
- 把延迟探针包装成带宽测速结果。
