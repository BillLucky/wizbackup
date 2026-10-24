# 离线恢复与迁移

[English](RESTORE.md) | [简体中文](RESTORE.zh-CN.md) · [README](../README.zh-CN.md)

## 恢复路径

```text
Need a quick inventory?       inspect
Have a complete ZIP?          restore into a new directory
Reading files need repair?   rebuild from verified retained objects
Want an editable workspace?  vault into a separate directory
Need old cloud revisions?    history (online, explicitly requested)
```

先激活环境，或使用 `wizbackup` 可执行文件绝对路径。断网前先安装依赖。这四个离线命令不需要 auth token。

## 1. 识别源资料

```sh
wizbackup inspect --source backups/account/personal
wizbackup inspect --source backups/archives/personal-snapshot.zip
```

Inspect 区分知识库或账号快照、普通 ZIW ZIP、ZIWR 密文。它报告可识别范围，密文不能算作成功转换的正文。

保留识别结果作为恢复记录。报告可能含私密标识或结构，不公开真实报告。

## 2. 校验 ZIP 指纹

把 ZIP 与同级 `.sha256` 文件放在一起：

```sh
cd backups/archives
shasum -a 256 -c personal-snapshot.zip.sha256
```

校验文件使用相对文件名，需要在 ZIP 目录执行。Linux 可用 `sha256sum -c`。没有 sidecar 只说明缺少这层指纹比对，不证明 ZIP 有效或无效。

## 3. 恢复到新目录

```sh
wizbackup restore --source personal-snapshot.zip --destination ../../recovery-restored
```

压缩包顶层目录保留。从 `personal/` 生成的知识库 ZIP，通常恢复为 `recovery-restored/personal/`。应读取结果返回的知识库路径，不假定每个包都使用同一名称。

```sh
wizbackup verify --output ../../recovery-restored/personal
```

Restore 拒绝危险路径、特殊文件、冲突条目、超过安全限额、非空目标目录。它检查 ZIP 结构/CRC 及可用校验和证据，再报告发现的库和本地校验结果。不上传或合并资料到已有云知识库。

## 4. 重建阅读副本

```sh
wizbackup rebuild --source ../../recovery-restored/personal --destination ../../recovery-rebuilt
```

Rebuild 读取含 `metadata/notes.json` 与原始对象/记录结构的单个知识库。先核对源证据，再使用原始 HTML/资源包、解密 ZIW 或协作资料生成新的阅读树。

重建结果面向阅读，不替代完整源档案，不承诺复制每个原件、评论、历史响应或证书。原始档案应单独保留。

只有密文但保留了匹配的受支持证书时：

```sh
wizbackup rebuild --source ../../recovery-restored/personal \
  --destination ../../recovery-rebuilt-with-decryption \
  --password-file ../../.private/decryption-password.txt
```

密码文件应私下保存，权限 `600`。源资料不修改。缺密码或资源时如实保留 locked/partial，不能凭空构造历史数据或补出不存在的原件。

## 5. 导出阅读 vault

```sh
wizbackup vault --source ../../recovery-restored/personal --destination ../../reading-vault
```

导出目录保留阅读树与同级镜像资源：

```text
reading-vault/
  readable/
    Example/
      Example--<note-id>/
        note.md
        index.html
        metadata.json
        original/
        attachments/
  external-resources/
```

在 Markdown 软件中打开 **vault 根目录**，不能只复制 `readable/` 的内容。同级目录之间的相对路径必须保持。

导出不改变档案，省略原始对象、证书和凭据存储。但其中仍有私密笔记正文和元数据，不是匿名公开版本。

## 一次有价值的恢复演练

1. 先演练合成 demo。
2. 把真实 ZIP 及指纹复制到本人控制的独立位置。
3. 更换父目录或库名，在新路径恢复。
4. 核对大小与哈希。
5. 断网抽查 Markdown/HTML。
6. 若资料中存在，至少覆盖有图、有附件、解密来源、协作来源的笔记。
7. 检查附件链接与笔记链接的实际目标，不仅看文件存在。
8. 记录缺资源、未支持类型、源异常、工具版本。

合成演练证明代码路径能工作，不证明某份真实档案完整；真实档案还需检查自己的报告和文件。

## 表达精度与云端恢复

Markdown 可能简化表格、手写、嵌入工具和复杂协作排版。原始 HTML、JSON 快照、附件与资源是未来改进转换的依据。

重建云账号、恢复访问权限、上传服务器、恢复删除内容是另行操作。本项目没有云端写回适配器。
