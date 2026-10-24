# 档案格式 v1

[English](FORMAT.md) | [简体中文](FORMAT.zh-CN.md) · [README](../README.zh-CN.md)

## 身份与路径

笔记由知识库 ID、笔记 ID、云端版本标识，标题用于展示。即使文件系统需要安全名称，原始标题与目录路径仍保存在元数据中。

`files[].path` 与 `readable_path` 相对于知识库根目录，是恢复的权威定位路径。转换器的诊断路径可能记录生成环境，恢复工具不应依赖它。

## 文件约定

| 路径 | 含义 |
| --- | --- |
| `metadata/notes.json` | 当前笔记清单，来自已完成的版本枚举与目录枚举并集 |
| `metadata/inventory-crosscheck.json` | 两种独立枚举的数量与差集 |
| `metadata/categories.json` | 目录响应，包括逻辑空目录 |
| `metadata/tags.json` | 标签名称、标识与层级 |
| `metadata/attachment.json` | 全库附件元数据 |
| `metadata/param.json` | 笔记参数 |
| `metadata/deleted.json` | 可取得的删除标记，不是本地删除指令 |
| `metadata/user-cert.json` | 加密证书数据，不是明文证书密码 |
| `objects/<id>/<version>/download.json` | 原始正文响应 |
| `objects/.../metadata.json` | 原始笔记元数据 |
| `objects/.../original.ziw` | 原生传输对象，需通过文件头判断 ZIW 或 ZIWR |
| `objects/.../decrypted.ziw` | 本机解密且通过内容/CRC 校验的 ZIP |
| `objects/.../content.ziw` | 用 HTML 与资源重建的包 |
| `objects/.../resources/` | 下载的普通笔记资源字节 |
| `objects/.../attachments/` | 原始附件传输字节 |
| `objects/.../collaboration/` | 快照 JSON、官方 HTML、资源、重建包 |
| `objects/.../comments.json` | 可取得的评论响应 |
| `objects/.../history.json` | 可取得的历史清单，不是历史正文 |
| `readable/` | 生成的 Markdown、离线 HTML、元数据、解出成员与附件 |
| `external-resources/` | 可选镜像、CSS 源响应、缓存清单与失败项 |
| `records/<id>.json` | 版本、状态、告警及每文件大小/SHA-256 |
| `catalog.json` 与 `index.html` | 生成的笔记定位表和本地导航 |
| `verification.json` | 最近一次本地完整性与阅读检查结果 |
| `progress.json` | 运行计数，不是完整性证明 |

账号级 `knowledge-bases.json` 记录各个隔离知识库根目录，`account-summary.json` 连接每库的备份、校验、后处理与压缩结果。

## 合成记录片段

以下仅用于解释字段，不是完整有效记录或真实账号：

```json
{
  "guid": "33333333-3333-4333-8333-333333333333",
  "version": 7,
  "status": "exported",
  "readable_path": "readable/Example/Example--33333333-3333-4333-8333-333333333333",
  "files": [
    {
      "path": "objects/33333333-3333-4333-8333-333333333333/7/index.html",
      "size": 123,
      "sha256": "EXAMPLE_SHA256_REPLACE_WITH_64_HEX_DIGITS"
    }
  ],
  "warnings": []
}
```

记录状态包括 `exported`、`locked`、`unsupported`、`failed`、`auth_required`。正文已导出仍可能有缺资源告警；密文可以保留而尚不可读。

## 附件校验

传输内容可能是原文件，也可能是旧版单文件 ZIP 包装。优先匹配原始字节校验和，避免把用户自己的 ZIP 误拆。

| 模式 | 含义 |
| --- | --- |
| `raw-md5` | 原始字节匹配标准 MD5 |
| `raw-legacy-md5` | 原始字节匹配旧 DWORD MD5 表示 |
| `wrapped-member-md5` | ZIP 唯一安全成员匹配标准 MD5 |
| `wrapped-member-legacy-md5` | 唯一成员匹配旧 MD5 表示 |

适配器分别保留原始字节和还原文件的 SHA-256。内容校验通过但大小元数据陈旧时，会记为大小异常，不会静默忽略。

## 例外保留

- `source_metadata_anomalies` 记录有证据支持的元数据差异，如稳定 CSS 响应、坏导航链接。
- `source_checksum_anomaly` 表示上游校验和仍未匹配。稳定密文只有满足重复下载、解密及 CRC 检查时才可恢复，校验和仍标失败。
- `export.missing_resources` 列出未解决的本地引用。
- `export.external_resources` 列出未解决的外部引用。
- `ignored_client_dependencies` 记录精确识别且未使用的客户端 UI 依赖，仅从阅读副本移除。
- ZIP 同名条目逐字节相同时可提取一次并计数；内容冲突、危险路径和特殊文件拒绝处理。
- 未知元数据应继续保留在原始 JSON 中，供未来适配器使用。

## 重定位与快照

改变父目录或知识库目录名，不改变权威相对路径。`readable/` 与同级 `external-resources/` 必须一起保留，vault 导出会维持此关系。

云端版本变化时保留旧对象目录。当前记录描述最近一次保留版本，带日期 ZIP 用于固定恢复时间点。SHA-256 文件标识 ZIP 字节，不提供数字签名或加密。
