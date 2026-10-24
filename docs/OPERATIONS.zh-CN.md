# 运行与排错

[English](OPERATIONS.md) | [简体中文](OPERATIONS.zh-CN.md) · [README](../README.zh-CN.md)

## 联网运行前

1. 使用源码目录外的私有工作目录。
2. 检查可用磁盘。原始对象、提取文件、阅读副本、ZIP 可能同时存在，空间不能只按压缩大小估算。
3. 使用本人会话凭据，auth 文件权限为 600。
4. 先验证小样本；存在加密笔记时先试一篇解密。
5. 使用新目录或续跑已知档案，避免备份/后处理任务重叠。

CLI 的目标锁防止同一目标重复运行。下载、镜像、索引、打包运行时，不要另行编辑阅读副本。

每次验收记录实际工具版本（wizbackup --version）及对应源码 tag/commit，后续不能仅凭旧进程号判断运行版本。

## 主流程

```sh
wizbackup discover --auth .private/auth.json
wizbackup backup-all --auth .private/auth.json --output backups/account --workers 4
wizbackup verify --output backups/account/personal
wizbackup serve --output backups/account --port 8765
```

建议从 4 个线程开始，CLI 上限为 8。遇到限流或服务端错误会退避。重复命令会复用已完成且校验通过的笔记，不需要先删目录。

`--limit` 用于样本。`--no-refresh` 使用已有清单，不能证明远端没有新增。枚举失败后，需要完成刷新和交叉核对，才能声明全量。

## 分阶段执行

```sh
# 先取得可访问的第一方对象，暂不处理外链。
wizbackup backup-all --output backups/account --no-postprocess

# 可选补全第三方图片/样式，并明确网络路由。
wizbackup mirror-external --output backups/account/personal --external-proxy direct

# 建立本地目录和已知笔记链接。
wizbackup catalog --output backups/account/personal

# 校验后生成新的快照。
wizbackup verify --output backups/account/personal
wizbackup pack --output backups/account/personal --archive backups/archives/personal-snapshot.zip
```

`pack` 拒绝已有压缩包目标。`.sha256` 文件应与 ZIP 一起保留。打包创建快照，不会修复缺失资源。

## 网络配置

| 配置 | 作用范围 |
| --- | --- |
| 默认 `direct` | 第一方云端请求不继承环境代理 |
| `--network us` | `http://127.0.0.1:7897` 的兼容别名，不验证出口地理位置 |
| `--proxy http://127.0.0.1:7890` | 本进程第一方请求显式 HTTP(S) 代理 |
| `--external-proxy direct` | 明确让外链资源直连 |
| `--external-proxy http://127.0.0.1:7890` | 为外链图片/样式单独指定代理 |

命令不修改系统代理。已选择代理失效时返回错误，不静默改用其他路由。代理 URL 不能包含用户名或密码。

## 进度

回环地址看板分别显示每库状态、笔记总数、外链资源进度和校验摘要，不列出或提供笔记内容。

```text
discovery -> inventory -> cross-check -> download
                                     -> optional mirror -> catalog
                                                        -> verify -> package
```

笔记计数 100% 后可能仍在补资源或压缩，需要同时看阶段。运行进度不能替代 `verification.json`。

## 挂机运行

在已激活环境的持久终端或本人控制的任务管理器中运行。启动后检查实际进程和持续变化的进度时间戳，不能仅凭后台启动命令成功就认定任务存活。

项目辅助脚本只是本机便捷工具，不是操作系统服务，也不保证开机自动恢复。重启后重新执行命令，已完成对象会先校验再复用。日志保存在私有工作目录。

## 故障处理

| 情况 | 处理 |
| --- | --- |
| 认证失效/拒绝访问 | 按正常方式刷新会话，更新私有 auth 文件，保留已有档案 |
| 证书密码错误 | 确认独立本机密码，密文与证书继续保留 |
| 目录/版本枚举不一致 | 保留两种清单并完成交叉核对，不凭 UI 数量猜完整 |
| 429 或临时 5xx | 允许有限退避，查看报告后以较小范围重试 |
| 历史列表可读、正文失败 | 保留列表和逐版本状态，稍后显式重试 |
| 外部图片失败 | 保留原 HTML、引用和失败记录，不用生成图冒充 |
| 上游 MD5 不同 | 查看源异常；本地 SHA 与 ZIP CRC 是其他层证据 |
| 本地 SHA 不同 | 保留报告，从已验证对象或另一已知快照恢复 |
| ZIP 拒绝解压 | 检查路径/类型/大小问题，不对未知档案关闭保护 |
| 手工复制后 vault 缺图 | readable 与同级 external-resources 一起复制，或使用 vault 导出 |

HTTP 200 仍可能是登录页、错误页或占位图，需要内容检查和来源证据。

## 退出码

| 退出码 | 含义 |
| ---: | --- |
| 0 | 完成命令声明范围 |
| 1 | 输入/I/O/认证/网络/处理错误 |
| 2 | 当前笔记缺失或离线源检查失败 |
| 3 | 核心档案已保留，仍有阅读、资源或源信息告警 |
| 4 | 目录索引完整性预检失败 |
| 5 | 历史正文获取未完成 |

离线命令分别报告完成度、源完整性、继承的阅读缺口。需要与恢复结果一起保留 JSON 报告。当前笔记成功不证明历史正文可取得。

## 后处理中断

阅读文件与记录是不同的文件系统对象。如果恰在修改阅读副本后、提交记录前中断，下次可能报告 `RecordIntegrityFailed`。工具不会把改变后的字节重新认证为旧记录。

保留原始档案。云端仍可访问时重跑备份，重新生成该篇。完全离线时用 `rebuild` 写入新目标，已验证原始对象仍可作为恢复输入。不要手改预期哈希来消除告警。

## 历史正文

```sh
wizbackup history --output backups/account/personal --guid 33333333-3333-4333-8333-333333333333
```

示例 ID 是合成值，应选本地清单中的笔记。此命令使用已认证云端会话和读取接口，不调用历史回滚。连续服务端错误会熔断当前笔记，其余版本保留 pending。服务状态可能改变，应保留可重试失败，不把错误解释成空历史。

## 增量使用

再次运行相同全量命令，发现当前可访问库和版本。保留带日期快照用于旧时间点。建议编辑独立 vault，保留生成阅读层作为校验基线。
