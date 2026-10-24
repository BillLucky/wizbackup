# 协议研究与归因

[English](RESEARCH.md) | [简体中文](RESEARCH.zh-CN.md) · [README](../README.zh-CN.md)

本文是互操作研究笔记，不是服务商 API 合约。实现区分协议证据、档案保留和可读转换；公开源码存在某个接口，不保证当前服务器支持它。

## 阅读入口

| 来源 | 相关内容 |
| --- | --- |
| [WizTeam/WizQTClient](https://github.com/WizTeam/WizQTClient) | 官方同步、对象打包、经典加密格式 |
| [famotime/Wiznotes_tools](https://github.com/famotime/Wiznotes_tools) | 社区网页导出流程与格式处理 |
| [cloudy-liu/wiz-to-obsidian](https://github.com/cloudy-liu/wiz-to-obsidian) | 社区远端/缓存补齐及 Markdown 迁移 |
| [altairwei/WizNotePlus](https://github.com/altairwei/WizNotePlus) | 桌面客户端继承关系与导出用例 |
| [yoke88/wiznoteExporter](https://github.com/yoke88/wiznoteExporter) | 早期归档与缓存解释 |

这些是源码阅读线索，不是维护排名或运行未审查代码的建议。引用项目及资产保留各自许可证，WizBackup 不打包其桌面客户端。

## 枚举与普通正文

账号服务和知识库服务分离。使用当前已认证账号返回的知识库地址，不猜测统一主机。

| 数据 | 读取接口 |
| --- | --- |
| 目录 | `GET /ks/category/all/{kb}` |
| 标签 | `GET /ks/tag/all/{kb}` |
| 版本笔记列表 | `GET /ks/note/list/version/{kb}?version={cursor}&count={n}&pageSize={n}` |
| 目录笔记列表 | `GET /ks/note/list/category/{kb}?category={path}&start={offset}&count={n}` |
| 附件清单 | `GET /ks/attachment/list/version/{kb}` |
| 删除标记 | `GET /ks/deleted/list/version/{kb}` |
| 正文与元数据 | `GET /ks/note/download/{kb}/{doc}?downloadInfo=1&downloadData=1` |
| 单篇附件列表 | `GET /ks/note/attachments/{kb}/{doc}` |
| 附件字节 | `GET /ks/object/download/{kb}/{doc}?objType=attachment&objId={att}` |
| 资源字节 | `GET /ks/object/download/{kb}/{doc}?objType=resource&objId={name}` |
| 个人证书 | `GET https://as.wiz.cn/as/user/cert` |

主源：[WizKMServer.cpp](https://github.com/WizTeam/WizQTClient/blob/7067bcfc421edce932aa3144ef2d1d631f27595a/src/sync/WizKMServer.cpp)、[WizKMSync_p.h](https://github.com/WizTeam/WizQTClient/blob/7067bcfc421edce932aa3144ef2d1d631f27595a/src/sync/WizKMSync_p.h)。

多篇笔记可共享版本号。因此单一最大版本游标不能证明页边界没有遗漏，独立目录枚举提供第二层核对。

普通响应可能把 `info`、`html`、`resources` 放在顶层，不能假定都在 `result` 中。官方现代客户端在没有原生 URL 时也会从 HTML 和资源重建 ZIP，WizBackup 对此明确标记。

HTTP 200 不能单独证明拿到正确对象。旧路由或缺失资源路由可能返回其他内容或占位图。应匹配可用元数据、内容类型/签名和压缩包校验，保留未解决差异。

## 经典 ZIWR v1

[WizZiwReader.h](https://github.com/WizTeam/WizQTClient/blob/7067bcfc421edce932aa3144ef2d1d631f27595a/src/share/WizZiwReader.h) 描述 156 字节头：

| 偏移 | 长度 | 字段 |
| ---: | ---: | --- |
| 0 | 4 | ASCII `ZIWR` |
| 4 | 4 | 小端版本号，当前支持 1 |
| 8 | 4 | 小端加密密钥长度，当前支持 128 |
| 12 | 128 | RSA 加密的单篇密码 |
| 140 | 16 | 保留字段 |
| 156 | 其余 | AES 加密 ZIP |

个人证书含 RSA 整数 `n`、`e` 和经密码加密的 `d`。兼容解密需要使用规定的 UTF-8/MD5 十六进制派生、固定 IV、CBC padding、RSA PKCS#1 v1.5，再验证输出 ZIP。

主源：[WizZiwReader.cpp](https://github.com/WizTeam/WizQTClient/blob/7067bcfc421edce932aa3144ef2d1d631f27595a/src/share/WizZiwReader.cpp)、[WizEnc.cpp](https://github.com/WizTeam/WizQTClient/blob/7067bcfc421edce932aa3144ef2d1d631f27595a/src/share/WizEnc.cpp)。

这是旧格式互操作解密，不是新加密存储的设计。CRC 不等于认证加密。缺证书密码或不支持的密钥格式应明确标失败/locked。

## 附件包装与旧 MD5

[WizMd5.cpp](https://github.com/WizTeam/WizQTClient/blob/7067bcfc421edce932aa3144ef2d1d631f27595a/src/share/WizMd5.cpp) 包含按四个小端 DWORD 输出的表示，区别于常见十六进制字节顺序。[WizDatabase.cpp](https://github.com/WizTeam/WizQTClient/blob/7067bcfc421edce932aa3144ef2d1d631f27595a/src/share/WizDatabase.cpp) 也说明了压缩附件传输处理。

适配器先检查原始字节的标准/旧 MD5。只有安全且唯一 ZIP 成员的校验和命中，才把它当作传输包装。用户自己的 ZIP 若原始校验命中，继续保持 ZIP。原始与还原 SHA-256 分开记录。

大小陈旧与校验和不匹配是两回事。内容校验匹配可以支持记录大小异常；未解决的上游 MD5 即使密文稳定且可解密，也仍需保留告警。

## 协作文档

公开 [wiz-editor 包](https://www.npmjs.com/package/wiz-editor) 提供额外协议线索。取得编辑器令牌后，正文操作为只读 GET：

```text
POST /ks/note/{kb}/{doc}/tokens       issue a token for existing access
GET  /editor/{kb}/{doc}/versions/latest?metaOnly=false&cookie=false
GET  /editor/{kb}/{doc}/export/html?os=false
GET  /editor/{kb}/{doc}/resources
GET  /editor/{kb}/{doc}/resources/{name}
```

快照、官方 HTML、资源清单是不同产物。资源列表可能含当前正文未引用资产。公开编辑器还描述了其他导出接口，源码中存在不代表 WizBackup 已实现。

社区[远端客户端代码](https://github.com/cloudy-liu/wiz-to-obsidian/blob/2269d28312a6397cc11112f6ff297f24c21bf53e/src/wiz_to_obsidian/wiz_remote.py) 描述其他读取路径。本实现使用 REST，不发送编辑操作或正文更新。

## 历史正文

```text
GET /ks/history/list/{kb}/{doc}?objType=document&objGuid={doc}
GET /ks/history/view/{kb}/{doc}?version={v}&editorGuid={editor}
```

历史查看可能需要客户端类型等参数。读取接口与历史回滚操作不同，本项目不调用回滚。

需要以实际服务器和账号核验可用性。列表成功时正文仍可能返回服务端错误。保留每版本 downloaded/locked/failed/pending、继续分页提示、有限重试结果。当前正文不能替代全部历史版本。

## 证据纪律

- 转换前保留原始响应。
- 源身份/版本与本地字节可追溯。
- 区分源校验和与本地 SHA-256。
- 保留失败与未支持状态。
- 公开测试、样例、日志和 issue 复现只使用合成数据。
- 归因协议来源，不把上游代码或资产改挂其他许可证再分发。
