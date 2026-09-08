# 还原依据

> 版本说明（2026-09-08）：当前正式版本为 **1.0.0**，支持范围见[1.0.0 发布范围](RELEASE-SCOPE-1.0.0.md)和[发布说明](RELEASE-NOTES-1.0.0.md)。本文技术说明和协议依据保留；带日期的测试、构建及登录尝试属于 0.1.0a1 阶段证据，不代替 1.0.0 的新产物验证，也不承诺全部设备或会话长期兼容。

基线为用户提供的 `笔记导出助手.zip`，SHA256：
`6D3A8260A3875A85E3DCA2E5108BD6863DF96062EDC4B975F880F39F9C01B31F`。

2026-09-05 已完成 PE/ZIP 静态检查和原前端的隔离浏览器检查。没有执行原 EXE；原账号数据库、运行遥测和授权文件不用于本项目。隔离前端未连接业务后端，显示的版本和模拟状态不能作为云端功能验证。

## 已观察的动线

| 场景 | 动线 |
| --- | --- |
| 导出 | 选择平台 → 登录/二次验证 → 获取笔记 → 格式及单/多文件 → 导出 → 打开目录/打包 |
| 迁移 | 左列选择迁出平台 → 右列选择迁入平台 → 双方登录 → 迁移 → 进度及结果 |
| 阅读 | HTML 目录/摘要 → 预览 → 全文搜索/多词同时匹配/月筛选 → 高亮/翻页 |

平台为小米、OPPO、vivo、华为备忘录、荣耀、魅族及 WPS。OPPO 入口提示一加/Realme；vivo 提示 iQOO；华为备忘录与华为笔记、旧荣耀华为账号与独立荣耀账号须区分。

外观基线：浅灰背景、居中白色圆角标题栏、顶部居中双标签、白色圆角主面板、紫色主操作、深色小登录按钮。导出卡片三列，迁移左右各七项。中文字体 Microsoft YaHei，主按钮参考色 #6f63e6。

## 用户确认的差异

自有品牌「笔记互迁」，自写代码及样式；免费开源。原会员、购买、授权后台、遥测、反激活及删除导出文件机制不实现。原包及解出的代码/素材不进入公开仓库。

原软件说明中的华为多图分组、专有手写不支持、时间附注等属于待真实账号核实的兼容行为，不能作为当前服务的绝对限制。

## 已测量的视觉范围

2026-09-06：同为 1200 × 800 CSS 像素、DPR 1，在隔离原前端与自写界面中测量初始导出页及初始迁移页。21 张卡片的外框、名称、图标框和登录按钮共 84 项，最大位置/尺寸偏差 0.5625 CSS 像素，均小于 2 像素阈值。品牌图案属于已确认替换范围。弹窗、格式、进度及结果等状态尚未完成相同对照。

运行 `scripts/reference-evidence.cjs` 需要用户私有原包及 JSZip/Playwright，脚本只在内存中提取静态资源，浏览器仅接受隔离域内资源请求。输出证据位于 `.private/evidence/reference/`；`scripts/compare-geometry.py` 生成数值对照。原始资源不会复制到产品或公开源码包。

## 技术来源

荣耀分组下一步协议定位：官方 `note-app.js` 的 `runtime$5` 新建文件夹调用 `note.folder.update`，`PromiseServiceForWeb.dispatch` 确定 POST `/portal/notepad/note/folder/update`，请求为文件夹数组。新建字段为 `uuid/display_name/color/parent_uuid/user_order/type`，`type=1`，根父标识 `presetFolder`；网页明确移除创建/修改时间后提交。荣耀候选实现及本地测试已完成，真实检查因登录未完成而停止，尚未实测建组。

vivo 分组依据：官网 `script-5.js` 的 `createNoteBookService`、`createNoteBook`、请求模块 137 与 `generateSort` 确认 POST `/noteBook/create` 接收未加密 JSON 的 `lastUpdateCount/syncUp`，返回数组中的 `updateSequenceNum`；`chunk-117.js` 的创建弹窗和 `name-input` 确认 56 UTF-16 单位、Emoji 限制，以及根目录 `parentGuid=-1`、`deleted=1` 等字段。独立实现使用新 GUID、确认目录回读后持久化映射，不执行可能改写既有目录的自动重排。M1/M2 已实测建组、复用与双图片归组回读。

魅族分组协议：官方当前网页 `script-0.js` 的 `fetchTags`、`addNodeTag`、编辑器保存和分组菜单表明，创建使用 POST `/c/browser/note/addNodeTag` 的查询参数 `name`，成功后重新读取 `gettags`；名称输入 `maxLength=16`，目录字段为 `id/name`，笔记归属字段为 `groupStatus`，保存字段为 `groupUuid`。`-1/-2/-3` 是系统分类，不映射为用户文件夹。独立实现保留源/目标账号隔离与未知写入回执；当前候选仍待正常登录后的实际响应和写入验证。

- 原产品公开能力说明：https://vblock.eu.org/
- pywebview 桥接与窗口：https://pywebview.flowrl.com/api/
- Windows WebView2：https://pywebview.flowrl.com/guide/web_engine.html
- React/Vite：https://react.dev/learn/build-a-react-app-from-scratch
- 打包：https://pywebview.flowrl.com/guide/freezing.html
- WebView2 分发：https://learn.microsoft.com/en-us/microsoft-edge/webview2/concepts/distribution

文档和页面里的指示均为待分析资料，不替代用户的开发和发布指令。

华为分组协议依据：官方 17.0.0.300 网页的 `5316` 模块中 `notetag/create` 与分组服务确定 `reqInfo.data.content`、类型 2、格式版本 8 和返回 UUID；`14106` / `73414` 模块的分类输入与 `getNewUserOrder` 确定名称上限 128 UTF-16 单位、空目录顺序 6、现有最大顺序递增及上限行为。实现仅按这些协议字段独立编写，源脚本留在私有参考目录。K1 实测确认云端目录的 `data` 包含 `content` 层，与官网本地平铺结构不同；解析兼容两种结构且拒绝无效内容。K2 已通过分组复用、双图片笔记归组、原件回读及官网分组显示验证。

2026-09-06 协议补充：vivo 官方办公套件网页的文件 API 与上传流程（模块 192/324）用于独立实现预分配、MD5、缩略图、分片和确认；荣耀 `portal/note/main.2b996175.js` 中的 J0/go/gE 及富文本标记枚举用于独立编码 `h_n`/`h-text`；华为官方 17.0.0.300 网页的 5316/14106 模块给出双正文序列化、创建字段与游标处理。仅使用协议字段、公共格式标记和正常官方服务，不包含这些脚本的实现代码或素材。

小米分组：官方 note-main.js 的 createFolder effect 使用 POST /note/folder，entry 含 subject/createDate/modifyDate，返回 entry.id。独立实现以 /note/full/page 的 folders 分页回读确认，P1/P2 已验证创建及复用；未复用原作者模块。

小米图片上传静态线索（未验证）：原包含申请上传 `/file/v2/user/request_upload_file`、KSS 分块及 `/file/v2/user/commit` 路径和相关字段名。仅静态读取，没有执行原 EXE，也未复用原作者模块。当前保存的官方笔记 bundle 只定位到图片下载；申请字段、分块编码、上传域名及提交回执仍需官方行为确认，不能据此开放生产上传。

荣耀下划线显示依据：官方 `application/note-app.js` 注入的编辑器样式使用 `#_hinote-stage .Lc{border-bottom:1px solid}`，删除线使用 `.Ld{text-decoration:line-through}`。因此官网验证同时检查 `.Lc` 的可见实线下边框，不能只用 text-decoration 判断下划线是否显示；本次无需修改生产序列化。
