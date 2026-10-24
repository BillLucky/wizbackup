# WizBackup

[English](README.md) | [简体中文](README.zh-CN.md)

为知笔记（WizNote）是一款中文云端笔记软件；WizBackup 是独立的本地备份、解密与迁移工具。

把当前可访问的为知笔记个人库、团队库归档到本地，保留原始资料，并在脱离服务端后重新生成可读笔记。

**使用 Agent？** 可以把仓库链接和 [Agent 上手指南](docs/AGENT_QUICKSTART.zh-CN.md)交给它，按技能安装和本机私密配置流程执行。

WizBackup 是独立开发的单向本地归档工具，包含云端下载、完整性清单、离线 HTML/Markdown 转换、本机 ZIWR 解密与离线恢复工具。原始数据和阅读副本分开保存。

**许可证：**[Apache-2.0](LICENSE)。**运行环境：**Python 3.11+。**平台：**macOS、Linux。CLI 目前不支持 Windows。

## 先做一次完全离线的演练

安装完成后，可以用合成笔记演练恢复流程，无需账号或网络连接：

```sh
git clone https://github.com/BillLucky/wizbackup.git
cd wizbackup
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.lock
python -m pip install --no-deps -e .

python examples/create_demo.py --output .demo
wizbackup inspect --source .demo/account/personal
wizbackup pack --output .demo/account/personal --archive .demo/personal.zip
wizbackup restore --source .demo/personal.zip --destination .demo/restored
wizbackup rebuild --source .demo/account/personal --destination .demo/rebuilt
wizbackup vault --source .demo/account/personal --destination .demo/vault
```

每次演练使用新的目标目录。恢复、重建、vault 导出不会覆盖已有非空目录。示例生成器只创建合成内容，不包含任何人的实际备份。

## 保留哪些内容？

| 内容 | 归档形式 | 边界 |
| --- | --- | --- |
| 个人与有权限的团队库 | 各自独立的清单、原始对象、阅读树、报告 | 仅限当前账号可访问的知识库 |
| 目录、标签、笔记参数、删除标记 | 原始 JSON 元数据 | 删除标记不能重建已永久删除的内容 |
| 普通笔记 | 原始接口响应、HTML、资源及明确标记的重建 ZIW | 现代接口可能只提供组成文件，不提供历史原 ZIP |
| 经典加密笔记 | 原始 ZIWR、加密证书、本机解密 ZIP、阅读副本 | 需要正确证书密码，不暴力破解或绕过安全措施 |
| 协作文档 | 结构化快照、官方 HTML 导出、完整资源清单 | 当前快照；静态导出未必保留嵌入应用的行为 |
| 附件 | 原始传输字节、校验后的可用文件、校验证据 | 识别旧 MD5 表示及经内容校验确认的单文件 ZIP 传输包装 |
| 阅读副本 | Markdown、离线 HTML、本地资源、每篇元数据 | Markdown 无法完整表达所有富文档功能 |
| 历史版本与评论 | 可取得的清单和响应，按篇受控重试历史正文 | 有历史清单不代表历史正文已下载 |
| 外链资源 | 可选图片/样式镜像及明确失败记录 | 认证、删除、网络策略、源站异常都可能阻止取得 |
| 恢复 | 识别、安全解压、重建阅读副本、导出 vault | 离线操作不恢复云账号及其权限 |

当前支持的 ZIWR 流程针对经典个人证书格式。独立企业证书、多套历史密钥、原始客户端 IndexedDB 自动解码尚未自动支持。

## 归档如何构建

```text
Authenticated WizNote account
  +-- personal library
  +-- accessible team / business libraries
                  |
                  v
  version inventory + independent folder inventory
                  |
                  v
  +---------------------+  +----------------------+
  | original objects    |  | original metadata    |
  | HTML / ZIW / ZIWR   |  | folders / tags / IDs |
  | images / attachments|  | dates / parameters   |
  +----------+----------+  +-----------+----------+
             |                         |
             +--- local conversion ----+
                         |
                         v
             Markdown + offline HTML
                         |
                         v
           records + SHA-256 verification
                         |
                         v
                ZIP64 + SHA-256 file
                         |
                         v
             inspect -> restore -> rebuild -> vault
                         (offline)
```

云端版本变化后使用新的对象目录。同步不会把云端删除操作传播为本地文件删除。阅读副本是生成的视图，原始资料需要保留，供未来转换器使用。

## 严格分开软件与私有数据

使用独立的私有工作目录。源码目录只应包含软件、文档和合成测试样例。

```text
workspace/
  wizbackup/                 public software checkout
  my-wiz-data/               private working directory
    .private/
      auth.json
      decryption-password.txt
    backups/
      account/
      archives/
```

### 推荐本机交互配置

激活环境后，在本机交互终端运行：

```sh
wizbackup setup --data-dir ~/WizBackupData
```

按本机提示操作，再使用返回的 next_command。登录密码不保存。Agent 无法提供本机 TTY 时，由你亲自在终端输入凭据，不在聊天中提供。参见 [Agent 工作流](docs/AGENT_QUICKSTART.zh-CN.md)。

### 手工配置会话

下面的替代流程使用上图中的同级私有目录：

```sh
mkdir -m 700 ../my-wiz-data
cd ../my-wiz-data
umask 077
mkdir -m 700 .private
```

使用文本编辑器创建 `.private/auth.json`：

```json
{
  "token": "EXAMPLE_NOT_A_TOKEN",
  "kb_guid": "11111111-1111-4111-8111-111111111111",
  "kb_server": "https://kb.example.invalid"
}
```

上面全部是合成占位值。请把每一项替换成**本人已登录的为知会话**返回的值。示例保留域名故意不可用：应使用为知返回的真实 HTTPS 知识库地址，不猜测统一服务器。

```sh
chmod 600 .private/auth.json
```

存在加密笔记时，把独立证书密码保存到 `.private/decryption-password.txt`，权限设为 `600`。通过编辑器等本机私密输入方式保存，不把密码放进命令参数、shell 历史、issue 或截图。

密码只在本机参与解密。无法解密时仍保留密文和证书。登录密码与证书密码可能不同。

## 备份账号

下面相对路径示例假定当前位于手工准备的私有目录。若使用交互 setup，应执行它返回的 next_command，使用已配置路径。


```sh
# 发现当前账号可访问的知识库。
wizbackup discover --auth .private/auth.json

# 个人和团队知识库分别归档。
wizbackup backup-all --auth .private/auth.json --output backups/account --workers 4

# 对配置中的个人知识库做小样本验证。
wizbackup backup --auth .private/auth.json --output backups/sample --limit 2
```

小样本不能算作全量备份。中断后重复同一个全量命令即可续跑。只有云端版本与本地文件哈希仍匹配的已完成笔记才复用。未完成文件会原子重下；这是按对象续传，不是 HTTP 字节范围续传。

会话失效后需要更新本机 token。本工具不要求关闭多因素认证。

### 网络选择

为知请求与外链资源请求都默认**直连**，不继承环境代理。

```sh
# 为知请求使用本次显式代理，外链仍直连。
wizbackup backup-all --output backups/account --proxy http://127.0.0.1:7890

# 单独配置外链资源代理。
wizbackup backup-all --output backups/account --external-proxy http://127.0.0.1:7890

# 兼容快捷名称：使用本机7897端口代理。
wizbackup discover --network us
```

`us` 名称不检测或保证代理出口的地理位置。代理 URL 不应含用户名或密码。配置只作用于本进程，不修改操作系统。

可用 `--no-postprocess` 优先保存为知对象，再单独运行 `mirror-external` 和 `catalog`。第三方请求使用独立会话，不携带为知凭据、Cookie 或笔记来源 URL。参见[安全边界](docs/SECURITY.zh-CN.md)。

## 阅读与校验

```sh
wizbackup serve --output backups/account --port 8765
# 浏览器打开 http://127.0.0.1:8765。

wizbackup verify --output backups/account/personal
wizbackup catalog --output backups/account/personal
wizbackup pack --output backups/account/personal --archive backups/archives/personal-snapshot.zip
```

看板只监听本机回环地址，仅显示计数，不提供笔记文件。阅读时打开各库本地的 `index.html` 或 `readable/` 中的文件。

下载成功、压缩包有效、离线阅读完整是三个不同结论。`verification.json` 区分本地完整性、未解密笔记、缺资源、上游校验异常及阅读告警。

## 档案目录

```text
backups/account/
  knowledge-bases.json
  account-summary.json
  personal/
    metadata/                   inventories and source metadata
    objects/<note-id>/<version>/
      download.json             original content response
      original.ziw              native object, possibly ZIWR ciphertext
      decrypted.ziw             locally verified plaintext ZIP
      content.ziw               reconstructed ordinary-note package
      attachments/              original transfer bytes
      collaboration/            snapshot, HTML, resources
    readable/<folder>/<title--id>/
      note.md
      index.html
      metadata.json
      original/                 extracted source members
      attachments/              verified usable attachments
    external-resources/         optional shared local mirror
    records/<note-id>.json      relative paths, sizes, SHA-256, state
    catalog.json
    index.html
    verification.json
  teams/<knowledge-base-id>/    same format, isolated library
backups/archives/
  personal-snapshot.zip
  personal-snapshot.zip.sha256
```

手工迁移时，`readable/` 与其同级的 `external-resources/` **必须一起保留**。`vault` 命令会复制阅读树及所需同级资源，不复制凭据或原始证书。

## 离线命令

| 命令 | 用途 |
| --- | --- |
| `inspect --source PATH` | 识别知识库目录、ZIP、ZIW 或 ZIWR，并汇总结构 |
| `restore --source SNAPSHOT.zip --destination NEW_DIR` | 安全恢复到新目录，校验 ZIP 及可用的校验和证据 |
| `rebuild --source LIBRARY --destination NEW_DIR` | 从已校验本地原始对象重建阅读副本，可选本机解密 |
| `vault --source LIBRARY --destination NEW_DIR` | 建立供 Obsidian 或其他 Markdown 工具使用的独立阅读目录 |
| `verify --output LIBRARY` | 校验已登记文件、身份、版本、阅读问题 |
| `history --output LIBRARY --guid NOTE_ID` | 显式重试某篇历史正文；此命令需要联网 |

Inspect 只读。恢复、重建、vault 导出写入新的目标目录，不需要云端 token。从密文重建时可能需要 `--password-file`。完整步骤见[恢复指南](docs/RESTORE.zh-CN.md)。

## 退出码与不完整内容

| 退出码 | 含义 |
| ---: | --- |
| `0` | 命令完成其声明的范围 |
| `1` | 输入、本地 I/O、认证、网络或处理错误 |
| `2` | 备份仍缺笔记或离线源检查失败 |
| `3` | 核心档案已保存，但阅读、资源、源校验仍有告警 |
| `4` | 目录索引的完整性预检失败 |
| `5` | 历史正文获取未完成 |

需要结合命令 JSON 报告判断，离线恢复尤其如此。本地 SHA-256 匹配说明已登记字节保持完整，不证明所有旧外链和历史版本仍存在。服务端 500、无法取得的外部图片、旧元数据不一致都会保留证据，不改写成成功。

## 文档

| English | 简体中文 |
| --- | --- |
| [Agent quickstart and skill installation](docs/AGENT_QUICKSTART.md) | [Agent上手与技能安装](docs/AGENT_QUICKSTART.zh-CN.md) |
| [Architecture and guarantees](docs/ARCHITECTURE.md) | [架构与完整性口径](docs/ARCHITECTURE.zh-CN.md) |
| [Archive format](docs/FORMAT.md) | [档案格式](docs/FORMAT.zh-CN.md) |
| [Operations and troubleshooting](docs/OPERATIONS.md) | [运行与排错](docs/OPERATIONS.zh-CN.md) |
| [Recovery and migration](docs/RESTORE.md) | [恢复与迁移](docs/RESTORE.zh-CN.md) |
| [Privacy and security](docs/SECURITY.md) | [隐私与安全](docs/SECURITY.zh-CN.md) |
| [Protocol research and attribution](docs/RESEARCH.md) | [协议研究与归因](docs/RESEARCH.zh-CN.md) |

[贡献说明](CONTRIBUTING.md) · [安全问题报告](SECURITY.md) · [更新记录](CHANGELOG.md)

测试使用合成数据和模拟传输，CI 不应需要真实为知账号：

```sh
python -m unittest discover -s tests -v
```

Apache-2.0 适用于软件；归档的笔记、附件与上游资产保留各自权利。为知笔记及其他产品名称属于其权利人。
