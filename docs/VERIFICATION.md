# 当前版本验证摘要

记录日期：2026-09-08（Asia/Shanghai）。产品版本：1.0.0。应用与测试源码基线：[`30a1a46`](https://github.com/lingxuanqjc-alt/note-bridge-desktop/commit/30a1a467faff44fbbf0a97116f5eac2620b432c1)。本轮仅补充文档、展示素材与 CI，不改产品逻辑。

## 本轮验证

环境：Windows 11 x64（build 26200）、CPython 3.13.15、Node.js 22.23.2、uv 0.12.5。Node ZIP 已与官方 SHA256 清单核对。

| 检查 | 本轮实际结果 | 证明范围 |
| --- | --- | --- |
| `uv sync --locked`、`npm ci --prefix ui` | 成功，锁文件未修改 | 干净 checkout 可安装指定依赖 |
| `ruff check src tests` | 通过 | 配置范围内的 Python 静态检查 |
| 三个所选离线测试文件 | **135 passed，0 skipped，9.64 秒** | 导出、回执可靠性、本地进程中断恢复 |
| `npm run build --prefix ui` | TypeScript 检查与 Vite 构建通过 | 前端可构建，不代替桌面桥接 |
| 实际前端＋合成桥接交互 | 5 项检查通过，0 页面异常，0 外部页面请求 | 格式入口、迁移方向、不兼容阻断、仅选兼容项、待核对提示 |
| 展示素材 | 三张截图已查看；GIF 5 帧/12.5 秒；WebM 15.68 秒且浏览器解码正常 | 仅为[合成界面演示](DEMO.md) |
| 远程 GitHub Actions | [PR #1 运行 34193927070](https://github.com/lingxuanqjc-alt/note-bridge-desktop/actions/runs/34193927070) 成功；**135 passed，29.34 秒**，Ruff 与 TypeScript/Vite 构建通过 | 对应 PR #1 的 head [`d7d9d55`](https://github.com/lingxuanqjc-alt/note-bridge-desktop/commit/d7d9d55b518a6c5dab48562aa150ea92ad7c350c)；其他提交应以各自实际 Actions 记录为准，不沿用本次结果 |

首次本地 pytest 因系统默认临时目录权限被拒绝而初始化失败；改用新的仓库临时目录时也曾因父目录尚未创建而失败。创建 `.cache` 并使用下列独立目录后，所选 135 项全部通过。未修改系统权限、产品源码或测试断言。

```powershell
New-Item -ItemType Directory -Path '.cache' -Force | Out-Null
$testTemp = Join-Path $PWD ('.cache\pytest-' + [guid]::NewGuid().ToString('N'))
.venv\Scripts\python.exe -m pytest -q tests/test_exports.py tests/test_reliability.py tests/test_process_recovery.py --basetemp $testTemp --tb=short -x
```

浏览器素材使用独立 Playwright 1.62.1 和临时浏览器环境采集。界面检查的“待核对”结果由合成桥接提供，不计为实际迁移结果。

## 复现最小检查

Windows、CPython 3.13、Node.js 22.23.2 和 uv 0.12.5；依赖按 `uv.lock`、`ui/package-lock.json` 安装。

```powershell
uv sync --locked --python 3.13 --no-python-downloads
npm ci --prefix ui --no-fund --no-audit
.venv\Scripts\ruff.exe check src tests
.venv\Scripts\python.exe -m pytest -q tests/test_exports.py tests/test_reliability.py tests/test_process_recovery.py
npm run build --prefix ui
```

[Offline checks](../.github/workflows/ci.yml) 在 pull request、main 推送或手动触发时执行同一最小范围。依赖安装需要联网；所选 Python 测试使用临时文件、虚构账号、内存目标和本地子进程，不需要厂商账号。前端 build 包含 TypeScript 类型检查。该流水线不发布安装包。

## 范围与限制

- 所选检查覆盖离线导出、重复写入防护、账号隔离、错误分类和进程恢复，不是完整 pytest 套件，也不是云端兼容性矩阵。
- 本轮未执行真实云端读写、WebView2 桌面桥接、安装/升级/卸载、手机同步或干净 Windows 10/11 验收。
- 独立 Markdown 渲染测试需要额外 Marked；浏览器交互另行验证，不混入上述 Python 测试数。
- 截图和短演示若展示合成状态，仅证明真实前端能显示这些状态；不证明目标云端已创建笔记。
- GitHub Actions 只有实际运行后才记录远程结果。本地通过不能代替远程 CI 成功。

## 历史证据

[原测试记录](TESTING.md)保留 0.1.0a1 阶段的执行范围、失败、数量与工件哈希；[1.0.0 发布范围](RELEASE-SCOPE-1.0.0.md)说明正式版的有限支持边界。`acceptance.json` 的原广泛验收状态保持真实，不因增加 CI 或展示素材而改写为通过。
