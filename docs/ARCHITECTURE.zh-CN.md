# 架构与完整性口径

[English](ARCHITECTURE.md) | [简体中文](ARCHITECTURE.zh-CN.md) · [README](../README.zh-CN.md)

## 设计目标

在原服务不可用后，仍能检查、校验、阅读和重新转换知识库。阅读副本是档案的一种实用视图，不是唯一保全格式。

## 组件

```text
cli
 +-- account discovery
 |    +-- personal library
 |    +-- authorized team libraries
 +-- inventory + download
 |    +-- version pages
 |    +-- folder pages / cross-check
 |    +-- raw responses, resources, attachments
 +-- local interpretation
 |    +-- ZIWR decryption
 |    +-- verified attachment materialization
 |    +-- collaboration snapshot / HTML
 +-- reading pipeline
 |    +-- safe extraction
 |    +-- Markdown / offline HTML
 |    +-- optional external-resource mirror
 |    +-- local catalog / note links
 +-- verification + ZIP64
 +-- offline toolkit
      +-- inspect
      +-- restore to new directory
      +-- rebuild from retained objects
      +-- export separate vault
```

## 三层数据

| 层 | 内容 | 作用 |
| --- | --- | --- |
| 源数据 | 原始响应、原生对象、HTML、资源字节、密文、协作 JSON | 证据及未来转换器的输入 |
| 阅读层 | 安全处理 HTML、Markdown、还原附件、本地路径 | 阅读与导入其他应用 |
| 控制层 | 清单、记录状态、身份/版本、SHA-256、例外、报告 | 完整性判断、续跑与恢复 |

对象按知识库、笔记 ID、云端版本隔离。同名标题不会互相覆盖。团队库各自拥有独立根目录，不合并进个人目录树。

## 枚举本身是正确性工作

版本号不是唯一笔记标识。直接以最大版本号加一翻页，可能跨过拥有同一版本号的其他笔记。下载器另外枚举目录，对照两种清单，并按笔记 ID 取并集。两种枚举都必须正常结束；请求失败不能解释成空库。

档案包含可取得的目录与标签元数据，包括逻辑空目录。即使解压工具未保留物理空目录，目录元数据仍是重建依据。

## 原始字节与重建

正文接口提供原生下载 URL 时，保留下载字节。现代普通笔记接口也可能只提供 HTML 与命名资源；由此构建的包会明确标为重建包，不冒充历史服务端原 ZIP。

协作文档除了 HTML，还需要结构化快照和资源清单。官方编辑器服务的 HTML 导出用于提高可读性，原始快照保留后续解析所需数据。

转换始终保留原件。本地解密写入新的 ZIP。外链本地化与笔记链接改写只作用于阅读副本及相应记录，不改原始云端对象。

## 四类不同检查

```text
HTTP transfer complete?  -> did the response arrive without truncation?
Upstream checksum match?-> did supplied metadata agree with those bytes?
Local SHA-256 match?     -> did recorded local bytes remain unchanged?
Reading complete?       -> are body, attachments and references usable?
```

ZIP CRC 用于检查损坏，SHA-256 文件用于标识内容，两者本身都不能认证不可信档案的作者。完整的本地对象仍可能包含无法访问的外部链接。

加密笔记的上游 MD5 不匹配时，恢复路径要求身份/版本相同、两次密文字节一致，并使用本人证书成功解密且整个 ZIP 的 CRC 通过。结果保留源校验异常和明确告警，不把 MD5 改称通过。

## 状态与续传

文件落盘并计算哈希后才推进笔记记录。中断对象使用临时文件，重跑该对象；已完成对象按版本和 SHA-256 复用。旧附件记录若缺少逐项校验证据，会先重建再允许复用。

云端删除只归档为标记，不因远端消失而删除本地历史。当前记录与阅读副本描述最近一次保留的备份；恢复更早时间点需要保留历史对象目录和带日期压缩包。

## 能力边界

- 云端正文操作保持只读。明确请求的本机 setup 可调用登录 POST，协作读取可用 POST 颁发编辑器令牌；两者不上传正文或修改权限。不实现正文上传、远端删除或历史回滚。
- 云端客户端面向允许的为知服务域名；任意自建服务器需要单独适配或配置。
- 支持经典个人 ZIWR 证书格式；独立企业证书和多套历史密钥需要单独适配。
- 历史清单、当前协作快照不等于完整历史正文。
- 永久删除资料、无权访问知识库、缺少密钥、外站移除资源不能凭元数据还原。
- 客户端 SQLite/IndexedDB 缓存可作为独立证据，但本版不解码所有客户端存储格式。
- 离线恢复不访问云端，必须继承原档案的告警，不能制造缺失数据。
