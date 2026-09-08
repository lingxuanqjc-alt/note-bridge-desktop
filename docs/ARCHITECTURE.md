# 实现与数据边界

> 版本说明（2026-09-08）：当前正式版本为 **1.0.0**，支持范围见[1.0.0 发布范围](RELEASE-SCOPE-1.0.0.md)和[发布说明](RELEASE-NOTES-1.0.0.md)。本文技术说明和协议依据保留；带日期的测试、构建及登录尝试属于 0.1.0a1 阶段证据，不代替 1.0.0 的新产物验证，也不承诺全部设备或会话长期兼容。

```text
ui/src                         React 页面、交互与类型
src/note_bridge
  app.py                       WebView2 桌面生命周期
  bridge.py                    仅向本地窗口暴露的 API
  models.py                    笔记、富文本、附件、任务模型
  providers/                   官方平台协议；未就绪能力明确关闭
  tasks.py / operations.py     单任务调度、取消、迁移确认记录
  storage.py                   账号隔离快照、任务和写入记录
  richtext.py / exporter.py    格式转换、安全输出、DOCX 目录
  reader.py                    独立 HTML 阅读器
packaging                      PyInstaller 与 NSIS
tests                          本地转换、故障和数据隔离验证
```

## 桥接合同

所有公开调用返回 `{ok: true, data: ...}` 或 `{ok: false, error: {code, message}}`。前端没有服务 Cookie、私钥或访问令牌字段。耗时请求返回任务，取消原生目录选择则返回 `null`。

| 调用 | 输入 | 输出 |
| --- | --- | --- |
| get_app_state | 无 | 平台共享状态、最近任务、可用输出 |
| login_platform / complete_login / cancel_login | PlatformId | 官方窗口／实际权限检查／清理状态 |
| fetch_notes | platform | TaskReport |
| export_notes | platform, format, multi_file | TaskReport 或 null |
| migrate_notes | source, target | TaskReport |
| get_task / cancel_task / open_task_output | task_id | 任务或操作结果 |
| open_directory | exports/resources/logs | 固定范围的系统目录操作 |
| package_notes | 空对象 | TaskReport 或 null |
| window_action | minimize/maximize/close | 操作结果 |

任务事件通过 `window.onNoteBridgeTask` 上报，并以定时状态读取补充。前端不能指定任意本地文件、任意请求 URL 或执行 Python。官方登录窗口没有应用 JS API。

## 持久化与故障

`notes` 和 `snapshots` 的键包含平台及账号。快照提交为 SQLite 事务；读取出错时不覆盖既有快照。附件缓存由平台、账号哈希和文件摘要划分，原始账号标识不进入前端。

迁移键包含来源平台、来源账号、笔记标识、内容摘要、目标平台及目标账号。发出写入前保存 `sending`，确认目标标识后保存 `confirmed`。未知网络结果保存 `uncertain`；重启后 `sending` 也转为 `uncertain`。确定性网络代码只对读取进行有上限的重试，不重试结果不明的创建请求。

vivo 与 WPS 在首次写入前通过任务上下文保存客户端分配的新笔记标识。写入状态变更和进程恢复保留该标识，便于核对。WPS 一次创建涉及三个远程步骤；首个文件已创建后，即使后续是明确拒绝或用户取消，也必须标记待核对，避免再次从头新建文件。

`resource_receipts` 将云文件标识关联到笔记写入记录：`allocated` 表示已分配，`uploaded` 表示上传已确认，笔记创建确认的事务才将其改为 `linked`。文件已分配后的失败统一保留待核对状态；没有自动删除云文件或重新上传未知结果的分支。vivo 原图按下载摘要核对，缩略图独立生成，不修改原图。云文件会话仅发往经验证的 vivo 域名。

导出使用所选目录内的新临时目录，完成全部写入后原子改名。只清理本次创建的临时目录；不删除既有导出。附件先限制路径，再校验摘要。HTML 从受控富文本模型生成，对文本转义、链接筛选，并设置 CSP；不直接插入平台原始 HTML。

## 平台开发约束

只依据正常登录后的官方服务行为和脱敏协议证据实现。公开网页中的参数编码可独立实现；不复用参考程序的密钥、许可服务或加密模块。没有协议证据的平台使用 `PendingProvider` 明确阻止操作，不能凭猜测写入用户账号。

`scripts/protocol-review.cjs` 是可选开发辅助，使用临时浏览器会话，只记录笔记请求的路径形状、字段类型和响应状态。它不记录查询值、请求头、Cookie、密码和笔记正文，不进入桌面运行包。
