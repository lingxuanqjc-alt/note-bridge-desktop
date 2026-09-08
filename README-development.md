# 笔记互迁 / note-bridge-desktop

> 版本状态（2026-09-08）：当前正式版本为 **1.0.0**，以[1.0.0 发布范围](docs/RELEASE-SCOPE-1.0.0.md)和[发布说明](docs/RELEASE-NOTES-1.0.0.md)为准。下文保留 **0.1.0a1 阶段的历史快照**；其中“当前”“预览”“待发布”、测试数量和工件 SHA256 均对应当时，不代表 1.0.0 的构建或发布审核结果。原广泛验收的缺项和真实登记状态保留，不改写为通过。

Windows 桌面笔记导出与跨平台迁移工具，Python 3.13 + pywebview 6.2.1 + WebView2，React + TypeScript。

**当前为 `0.1.0a1` 开发预览，正式发布验收尚未完成。** 源码及 BI 安装版、便携版均开放小米、OPPO、vivo、华为备忘录、荣耀、魅族、WPS 七个受限开发预览迁入目标。PNG/JPEG 原件、账号隔离、跨版本未知回执保护继续强制生效；小米限已核实非加密账号及单张 4 MiB。OPPO 大文件分片未完成实测，不宣称全面支持。 BI 七目标开发预览已通过 1685 项完整测试、前端构建、PyInstaller/NSIS；便携包 2,349 个文件逐一校验一致，本机 Windows 11 中文路径安装、升级、卸载保留导出等 9/9 项通过。新冻结 EXE 的独立登录子进程、会话隔离及临时目录清理已用本地合成页面验证；该检查不访问厂商或个人资料，也不代表厂商会话永久有效。

2026-09-07 BI：12 个 OPPO 相关方向的 24 条固定样例完成目标及源端后置核对；非 OPPO 的 30 个方向由用户确认后冻结。OPPO 原始综合样例 OR1 与六个迁入双条，共 13 条、24 张图片，已完成精确身份、版本、分组、完整非空白正文顺序、目标保留样式、结构及图片解码的官网核对。12 条受影响样例已作限定格式修复，BI5[0] 无需修改；代码显示降级单列，不算代码布局原样保留。 源端后置“未变”只在各原始证明的观察时点及固定范围成立。此后 OR1 与部分既有 OPPO 目标进行了有独立授权和证据的格式标记修复，版本随之变化；不能把旧来源指纹／版本证明写成当前永久未变。修复只更新既有样例，不新增笔记，旧失败、原始源端证明和修复证明分别保留。 版本保持 `0.1.0a1`，`release_ready=false`。正式迁移登记仍为 0/42；用户确认与固定样例通过不等于正式 42 方向及桌面环境全部通过。

## 当前可用内容

- 独立实现的导出页、迁移页、平台选择、官方登录窗口和共享会话状态。
- SQLite 按平台和账号隔离缓存；任务进度、取消和逐条问题报告。
- TXT、Markdown、HTML、DOCX 的合并／多文件导出、附件目录和 ZIP 打包。
- 离线 HTML 目录、摘要、多个关键词同时匹配、月份筛选、搜索高亮和分页。
- DOCX 合并导出的可跳转目录；安全文件名、原子目录提交和相对资源路径。
- 迁移前读取一次来源，逐条显示兼容性并选择范围；向目标新增笔记，保留源数据。
- 已确认版本自动跳过；同一源笔记的旧版本结果不明时，修改正文也不能绕过保护。迁移记录提供按当前双方账号隔离的只读回执查看。

详细范围见 [兼容性矩阵](docs/COMPATIBILITY.md)、[实施检查点](docs/CHECKPOINT.md)、[验收证据](docs/TESTING.md) 和 [中文指南](docs/GUIDE.md)。

## 本地运行

Windows 10/11 x64，已安装 Microsoft Edge WebView2 Runtime。源码环境需要 Node.js 20.19+（20.x）或 22.12+，以及 uv；Python 默认安装在项目的 `.tools/python` 中。

```powershell
powershell -ExecutionPolicy Bypass -File scripts/setup.ps1
```

如果 uv 没有加入 PATH，可传 `-UvPath 'uv.exe的完整路径'`。已安装官方 CPython 3.13 时，可传 `-PythonPath 'python.exe的完整路径'`，脚本会使用该解释器而不下载 uv 管理的 Python；适用于系统策略不允许 uv Python 发行版的环境。该选项面向正常安装的 CPython，不依赖开发机的嵌入式 Python 或私有路径。准备完成后双击 `启动笔记互迁.cmd`。

日常启动不需要运行开发服务器。应用默认将缓存和任务报告放在 `%LOCALAPPDATA%\NoteBridge`；`NOTE_BRIDGE_DATA_DIR` 可为本地开发测试指定独立数据目录。登录会话只在本次运行中使用，不会写入项目缓存或导出文件。

## 验证与构建

```powershell
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\ruff.exe check src tests
npm run build --prefix ui
.venv\Scripts\python.exe -m note_bridge.app --smoke-test
powershell -ExecutionPolicy Bypass -File scripts/build.ps1 -NsisPath 'makensis.exe的完整路径'
```

构建脚本生成 `dist/NoteBridge/NoteBridge.exe`、便携 ZIP、NSIS 安装包、源码 ZIP 和 SHA256 校验值。NSIS 使用 3.12；其官方来源为 https://nsis.sourceforge.io/Download 。安装程序检测 WebView2，缺失时引导前往微软官方页面。

`scripts/ui-evidence.cjs` 用独立 Playwright 浏览器验证界面合同，里面的模拟桥接只属于测试脚本；不会打入产品。`PLAYWRIGHT_MODULE_PATH` 可指定本机 Playwright 模块路径。真实云端验收与模拟测试分开记录。

`tests/test_migration_progress_ui.cjs` 与 `tests/test_receipt_review_ui.cjs` 使用源码前端和模拟桥接，可在完成前端依赖安装后分别用 Node.js 运行；还需 Playwright 和本机 Chrome，`CHROME_PATH` 可指定浏览器。迁移进度测试不要求私有原包：仅当显式设置 `REFERENCE_GEOMETRY_PATH` 时才额外对照该文件中的原包初始几何数据，否则会记录此项未执行，不计为参考还原通过。

## 源码边界

自写代码采用 MIT。第三方依赖及许可见 `licenses/`。主图标是原创几何图形，平台以文字识别。没有使用原作者的 Python 代码、授权服务、加密模块或图像资源。

原参考 ZIP、隔离解析结果、私人笔记、网络抓包、缓存和凭据均不进入源码包。正式发布要求 `docs/acceptance.json` 的真实矩阵和桌面检查全部通过，再由用户指示公开推送与 Release。

七个平台均有分组与归组接口样例证据，逐方向范围见 [迁移矩阵](docs/MIGRATION-MATRIX.md)。版本保持 `0.1.0a1`，`release_ready=false`。正式迁移登记仍为 0/42；用户确认与固定样例通过不等于正式 42 方向及桌面环境全部通过。 当前工件见 [交付审阅](docs/REVIEW-PACKAGE.md)。
