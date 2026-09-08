# 参与贡献

欢迎修复可以复现的问题、补充合成内容样例与改进使用说明。提出改动时，请说明具体使用场景、预期结果和当前结果；优先提交最小修改。

## 本地开发

环境准备、启动与构建见 [README-development.md](README-development.md)。本地测试使用合成数据；真实云端读写只在账号所有者明确授权和限定范围内进行。

```powershell
.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider --basetemp .private\contribution-test-01
.venv\Scripts\python.exe -m ruff check src tests
npm run build --prefix ui
```

每次测试选择一个新的临时目录，保留失败证据。部分独立渲染检查还需要 Chrome、Playwright 或指定的 Node 渲染依赖，按相关测试文档准备；不要把未执行的检查写成通过。

## 提交内容

- 测试应证明要保护的用户结果，例如不漏笔记、不串账号、未知写入不重发。
- 使用自己生成的笔记、图片和附件构造样例；不要加入私人笔记、参考原程序、会话或抓包。
- 协议差异、格式降级和验证范围必须写清；不以模拟数据代替真实云端验收。
- 提交代码遵循现有 MIT 许可与第三方许可边界；发布和版本升级由维护者另行确认。

若问题涉及安全或敏感数据，请先阅读 [SECURITY.md](SECURITY.md)。
