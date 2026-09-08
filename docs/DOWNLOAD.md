# 下载、启动与手机同步

从 [1.0.0 正式版本页](https://github.com/lingxuanqjc-alt/note-bridge-desktop/releases/tag/v1.0.0) 的附件区下载。

## 选择文件

| 文件 | 适合的用法 |
| --- | --- |
| [NoteBridge-1.0.0-setup.exe](https://github.com/lingxuanqjc-alt/note-bridge-desktop/releases/download/v1.0.0/NoteBridge-1.0.0-setup.exe) | 安装到电脑，通过桌面快捷方式启动 |
| [NoteBridge-1.0.0-windows-x64.zip](https://github.com/lingxuanqjc-alt/note-bridge-desktop/releases/download/v1.0.0/NoteBridge-1.0.0-windows-x64.zip) | 便携使用，完整解压后双击 `NoteBridge.exe` |
| [note-bridge-desktop-1.0.0-source.zip](https://github.com/lingxuanqjc-alt/note-bridge-desktop/releases/download/v1.0.0/note-bridge-desktop-1.0.0-source.zip) | 阅读或自行构建源码，不能作为安装包直接运行 |
| [SHA256SUMS.txt](https://github.com/lingxuanqjc-alt/note-bridge-desktop/releases/download/v1.0.0/SHA256SUMS.txt) | 核对下载文件是否与发布附件一致 |

系统要求为 **Windows 10／11 x64**，需要 Microsoft WebView2 Runtime。安装程序会检测运行时，缺少时引导到微软官方页面；便携版也需要相同运行时。

## 第一次启动

1. 使用安装版，或将便携 ZIP 解压到固定目录。便携版不要只取出 EXE，也不要直接在压缩软件里运行。
2. 打开“笔记互迁”，选择需要导出或迁移的平台。
3. 在软件打开的官方窗口中登录并进入笔记页，回到软件完成账号检查。
4. 先选择少量笔记，查看正文、图片与格式差异，再处理更多内容。

浏览器中已有的登录状态不等于软件已登录。退出应用后会清除会话，下次使用需要重新登录。登录页无法继续时，使用该窗口的“页面”菜单刷新或重新打开官网，详见[使用指南](GUIDE.md)。

## 校验文件

将附件和 `SHA256SUMS.txt` 放在同一目录。在该目录打开 PowerShell，例如核对安装包：

```powershell
Get-FileHash -LiteralPath '.\NoteBridge-1.0.0-setup.exe' -Algorithm SHA256
```

将输出与清单中同名文件对应的 SHA256 比较；字母大小写不影响结果。若不一致，重新下载对应附件。校验值用于发现文件变化，不等同于发行者数字签名。

## 手机端如何收到笔记

1. 来源云端已有笔记即可；尚未上传的内容，需要先从来源手机开启云同步并等待上传。
2. 电脑软件完成来源云端到目标云端的读取、转换及迁入。
3. 目标手机使用同一目标账号，在对应笔记应用开启云同步，由厂商服务同步到手机。

目标云端已有笔记但手机尚未显示时，先检查账号、云同步状态和网络，并确认手机应用对应的是同一项云服务。暂时没有显示不能直接证明迁入失败，请先核对结果，避免重复提交相同迁移。

软件无法强制手机同步，也不能凭云端写入成功认定手机已经收到。回执处理及内容差异见[使用指南](GUIDE.md)与[兼容性说明](COMPATIBILITY.md)。
