# 公开版本历史

## 2026-09-07 · Windows EXE 公开分发

- 公开仓库 `main` 已包含源码、公共架构文档、配置模板、测试和 `release/ClashNodeMonitor.exe`。
- EXE 通过 Git LFS 分块上传，避免普通 Git 大文件推送在网络链路中断；GitHub 直接下载仍提供完整 Windows 可执行文件。
- 验证证据：19 个 Python 单元测试、Python/JavaScript 语法检查、EXE 本机 API 启动冒烟均通过。

## 2026-09-07 · Public standalone release

- 将节点监控整理为可独立分发的 Clash/mihomo 本地软件。
- 统一公开命名为 `Clash Node Monitor`，移除私人节点、内部项目路径和外部项目专属文案。
- 增加自动连接发现、连接高级设置、连接测试、深色/浅色主题和 EXE 启动入口。
- 保留真实采样历史，并增加按 1/3/7 天范围导出原始 UTF-8 CSV 的能力。
- 提供 Windows 构建脚本、启动批处理、公共配置模板和无私人数据的架构文档。
- 验收范围：Python 语法检查、标准库单元测试、Web 静态资源检查、EXE 启动与本机 API 冒烟。
