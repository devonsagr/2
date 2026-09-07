# Clash Node Monitor 项目约定

## 项目范围

- 这是一个可独立分发的 Windows 本地软件：Python 本地服务、原生 Web 控制台和可选的桌面浮点入口。
- 运行时只通过 Clash/mihomo External Controller 读取节点状态；默认只绑定本机回环地址。
- 公开仓库不得包含真实配置、API 密钥、SQLite 数据库、缓存、用户路径或内部项目名称。

## 运行与验证

- 源码控制台：`py clash_node_monitor.py --server --open-browser`
- 单次采样：`py clash_node_monitor.py --once`
- 无界面常驻：`py clash_node_monitor.py --no-ui`
- 单元测试：`py -m unittest discover -s tests -v`
- 语法检查：`py -m py_compile clash_node_monitor.py`
- 构建 EXE：`powershell -ExecutionPolicy Bypass -File .\build_exe.ps1`

## 发布边界

- `run_monitor.bat` 优先启动 `release/ClashNodeMonitor.exe`；没有 EXE 时回退到本机 Python。
- EXE 使用 PyInstaller 把 `web/` 静态资源一起打包，启动后自动打开本地控制台。
- 公开配置模板只能使用示例地址和空密钥；真实配置文件由用户在本机生成并被 `.gitignore` 忽略。
- 浅色/深色主题、连接引导和 CSV 导出属于公开产品能力，修改时要同步更新 README 与架构文档。

## 安全与数据

- 监控数据只保存到用户本机 `data/node_monitor.sqlite3`，不会上传或调用云端服务。
- API 密钥只能留在本机配置或运行时内存，不得打印、提交或放入前端响应。
- 自动路由默认关闭；只有用户明确开启并满足安全条件时才允许调用 Clash 选择接口。
- 任何新功能都必须保持单一采样服务，避免打开多个页面时重复采样。
