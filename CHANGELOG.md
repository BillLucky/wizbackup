# Changelog / 更新记录

## 0.1.1 — 2026-10-24

### English

- Use the declared isolated build environment in CI, rather than a runner's missing or outdated setuptools backend.
- Verify source and wheel builds on macOS/Linux across Python 3.11, 3.13, and 3.14; tag pushes reuse the validated commit instead of duplicating the matrix.

### 简体中文

- CI 使用项目声明的隔离构建环境，避免依赖运行器缺失或过旧的 setuptools。
- 在 macOS/Linux 的 Python 3.11、3.13、3.14 上验证源码和 wheel 构建；标签复用已验证提交，避免重复运行矩阵。

## 0.1.0 — 2026-10-24

### English

- First public source release under Apache-2.0, with separate software and private-data workflows.
- Personal and authorized team-library discovery, version/folder cross-checking, object-level resumption, and local SHA-256 records.
- Retained source HTML/JSON/resources, local classic ZIWR decryption, collaboration snapshot/HTML/resource archive, and verified legacy attachment handling.
- Offline Markdown/HTML reading trees, internal note-link catalog, optional external-resource mirror, and ZIP64 snapshots.
- Offline inspect, safe restore, reading rebuild, and independent vault export.
- Loopback-only progress dashboard and privacy-preserving network routing.
- Synthetic tests/examples, equivalent English/Chinese documentation, and an installable Agent workflow.
- Explicit locked, unsupported, incomplete-resource, upstream-checksum, and historical-body states.

### 简体中文

- 首个 Apache-2.0 公开源码版本，软件与私有数据工作流隔离。
- 个人和已授权团队库发现、版本/目录交叉核对、按对象续跑、本地 SHA-256 记录。
- 保留源 HTML/JSON/资源，经典 ZIWR 本机解密，协作快照/HTML/资源归档，旧附件经校验还原。
- 离线 Markdown/HTML、内部链接目录、可选外链镜像、ZIP64 快照。
- 离线识别、安全恢复、阅读重建、独立 vault 导出。
- 仅回环地址进度看板与隐私优先的网络路由。
- 合成测试/样例、等价中英说明、可安装 Agent 工作流。
- 明确记录未解密、未支持、资源不全、上游校验异常、历史正文未取得等状态。
