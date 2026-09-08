# 项目案例：写入结果不明时，如何避免重复迁移

[返回项目](../README.md) · [当前验证](VERIFICATION.md) · [架构说明](ARCHITECTURE.md)

## 问题与范围

换手机品牌时，原有云笔记不容易一起带走。笔记互迁把“跨平台迁入”和“导出为可离线阅读的文件”放在同一个 Windows 工具中。用户可以选择具体笔记、查看格式差异，并保留来源内容。

这个项目关注需求定义、范围取舍、使用体验和验收。这里记录可追溯的产品判断和实现证据；现有证据不包含外部用户规模或所有平台全面验收。

一个关键取舍是：迁移遇到超时，不能简单显示“失败，请重试”。请求可能已经在目标端创建笔记，只是成功回执没有回到电脑。此时自动重发，可能生成重复笔记。

## 处理方式

迁移使用确定性的状态与持久化回执判断是否允许写入；路由、重试和状态分类都由代码明确处理。

| 观察到的状态 | 后续行为 | 用户得到的保护 |
| --- | --- | --- |
| 尚未发送 | 先保存写入意图，再调用目标创建接口 | 进程中断后仍知道请求曾经开始 |
| 已收到成功回执 | 保存目标标识，恢复时跳过同一版本 | 避免重复创建已完成的笔记 |
| 发送中断或结果不明 | 保留回执并显示“需要核对”，阻止自动重发 | 不用一次超时推断目标不存在 |
| 明确知道没有创建 | 记录拒绝及原因 | 与“可能已写入”保持区别 |

来源平台、来源账号、来源标识、内容指纹、目标平台和目标账号共同确定版本身份；来源或目标账号改变时不会复用另一账号的回执。对同一来源的旧版本未知写入也保留保护，修改正文不能用来绕过待核对状态。回执页面只展示本地记录，不把目标缓存命中当作云端内容完整的证明。

可以从这些代码入口核对实际行为：

- [迁移操作](../src/note_bridge/operations.py)：检查旧回执、保存意图、调用创建及确认/未知状态处理。
- [回执身份](../src/note_bridge/receipt_identity.py)与[存储](../src/note_bridge/storage.py)：账号和内容版本、持久化以及跨版本检查。
- [界面](../ui/src/main.tsx)：`ReceiptReview` 与 `needs_review` 提示。

## 为什么这些测试重要

测试不仅验证一次正常迁移能完成，还要回答“用户在最不合适的时刻关闭程序，会不会损失记录或重复写入”。

| 场景 | 自动检查的意图 | 可查看的证据 |
| --- | --- | --- |
| 同一笔记再次执行 | 已确认项只创建一次，来源保持不变 | [可靠性测试](../tests/test_reliability.py) |
| 目标可能已收到请求，但回执未知 | 后续执行保持待核对，创建函数不再次调用 | [可靠性测试](../tests/test_reliability.py) |
| 已写入本地合成目标后，强制终止实际子进程 | 重开 SQLite 保留账号缓存与资源标识，不重发未知写入 | [进程恢复测试](../tests/test_process_recovery.py) |
| 导出重名、空标题、中文和附件后搬到新目录 | 不覆盖笔记，正文和资源仍可到达 | [离线导出测试](../tests/test_exports.py) |

42 种内存目标组合只证明迁移逻辑；56 种离线导出组合只证明转换逻辑。它们都不能代替厂商云端接口、账号、原生排版或手机同步的实测。当前运行结果和未覆盖范围统一放在[验证摘要](VERIFICATION.md)，历史记录仍按原版本保存。

## 已交付与下一步

项目已提供 [Windows 1.0.0 安装包与便携包](https://github.com/lingxuanqjc-alt/note-bridge-desktop/releases/tag/v1.0.0)、使用指南、兼容性说明和源码。首次公开提交是整理后的发布快照，因此不把该提交数用作开发投入的证明。

接下来优先收集可复现的兼容性问题和真实使用反馈，按“问题 → 合成回归样例 → 修复 → 对应版本验证”的顺序维护。任何成功率、用户数或节省时间，都在取得实际证据之后再报告。

## English summary

Note Bridge is a Windows application for note export and cross-platform migration. This case study explains one reliability decision: a timeout after a remote write must remain uncertain instead of triggering a blind retry. Durable receipts, account-scoped identities and process-recovery tests make that decision inspectable. Synthetic tests and UI demonstrations are explicitly separated from real cloud validation.
