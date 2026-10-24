# 让 Agent 使用 WizBackup

[English](AGENT_QUICKSTART.md) | [简体中文](AGENT_QUICKSTART.zh-CN.md) · [README](../README.zh-CN.md)

## 给出仓库链接和明确任务

仓库链接用于定位软件，本身不代表授权账号访问、公开上传、披露凭据或删除文件。可以给 Agent 这样的范围说明：

```text
阅读 https://github.com/BillLucky/wizbackup 以及仓库中的
skills/wizbackup/SKILL.md。

帮助我备份当前账号可访问的个人与团队为知知识库。
源码目录与 ~/WizBackupData 分开存放。
通过本机 setup 流程配置凭据，不让我在聊天中粘贴密码或 token。
加密笔记需要证书密码时，让我使用本机私密文件或受支持的本地输入。
保留原始密文，能解密的内容在本机生成阅读副本。
报告数量、校验结果、不可用资源和历史版本缺口。
不公开笔记，不修改云端正文或权限，不删除文件。
```

Agent 应先读当前 README、仓库约定、技能，再选命令。核实实际 checkout、版本、目标目录、正在运行的任务，不沿用旧机器路径或 PID。

## 安装程序

```sh
git clone https://github.com/BillLucky/wizbackup.git
cd wizbackup
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.lock
python -m pip install --no-deps -e .
```

执行辅助脚本前先审查仓库。技能是操作手册，不替代 Python 程序安装。

如果 Agent 每次工具调用都开启新 shell，后续命令应使用虚拟环境可执行文件的绝对路径。激活状态不会自动跨独立调用保留。执行 setup 参数数组时，通过该环境定位程序，不把结果拼接成 shell 字符串求值。

## 安装或复制技能

Codex 用户级安装：

```sh
python scripts/install_skill.py --destination ~/.agents/skills/wizbackup
```

目标是**技能目录本身**，不是父目录。安装器拒绝已有目标；替换前先检查旧版本，不盲目删除其他技能。

默认位置依据当前 [Codex 技能文档](https://learn.chatgpt.com/docs/build-skills)。仓库级技能发现及不同宿主行为，应以宿主当前文档为准。

其他 Agent 宿主使用其支持的目录，显式指定：

```sh
python scripts/install_skill.py --destination ./agent-skills/wizbackup
```

再按该宿主的正常机制加载或引用 `agent-skills/wizbackup/SKILL.md`。本文不猜测 Claude 或其他产品的全局安装路径。也可手工复制技能目录，保留配套文件。

## 配置私有数据目录

激活环境后，在**本机交互终端**运行：

```sh
wizbackup setup --data-dir ~/WizBackupData
```

Setup 根据所选方式接收本机交互账号登录或会话 token 信息。密码不回显，不保存登录密码。Setup 只配置凭据，不启动备份。结果 JSON 提供 `next_command` 参数数组，Agent 可使用文件路径继续，不需要接触秘密值；应审核后按参数执行，不把响应文本当 shell 代码求值。

如果 Agent 的工具无法提供本机 TTY，应让你亲自运行该命令，只回复完成或脱敏错误，不改成在聊天里收密码。

也可在不显示文件内容的情况下导入本人已授权的 auth 文件：

```sh
wizbackup setup --data-dir ~/WizBackupData --from-auth /path/to/authorized-auth.json
```

路径仅是示例。只导入本人拥有且明确授权的文件，不扫描浏览器或其他项目寻找凭据。

证书密码可能不同于登录密码。按受支持的本机提示输入，或按 README 保存私密密码文件。缺密钥应保留 locked，不是猜密码或降低认证强度的理由。

## 执行与验收

Agent 应检查 setup 给出的 `next_command`，确认账号、目标、范围符合授权，然后：

1. 发现当前可访问知识库；
2. 做小样本，包括适用时的本机解密；
3. 执行获授权的全量备份；
4. 区分本地完整性与阅读缺口；
5. 创建压缩包，记录 SHA-256/CRC 结果；
6. 在新目录恢复演练；
7. 报告仍不可取得、未解密或未支持的内容。

```text
repository + skill
        |
        v
isolated code install
        |
        v
local interactive setup ------> private data directory
        |                               |
        v                               v
authorized backup --------------> retained objects
                                        |
                                        v
                                verify + archive
                                        |
                                        v
                              offline recovery drill
```

可以通过仅回环地址看板读取进度。看板只有计数，不提供笔记下载。

## Agent 必须保留的边界

- 私有数据放在公开 checkout 外，不进提交、issue、release 附件或聊天日志。
- 不打印秘密文件内容，也不复制到回复。
- 不改云端正文或权限。明确请求的本机登录和编辑器令牌颁发是认证步骤，不是正文写操作。
- 网络路由逐进程明确选择，不改全局代理、不降低多因素认证。
- 保留资源不完整与源异常状态。
- 保留原件，恢复与可编辑 vault 使用独立目标。
- 发布软件不意味着发布用户的档案。

手动使用见[主说明](../README.zh-CN.md)，恢复见[离线恢复](RESTORE.zh-CN.md)。
