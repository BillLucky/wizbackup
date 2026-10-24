# Contributing

[English](#english) | [简体中文](#简体中文)

## English

Start with [architecture](docs/ARCHITECTURE.md), [archive format](docs/FORMAT.md), and [security boundaries](docs/SECURITY.md).

### Development setup

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.lock
python -m pip install --no-deps -e .
python -m unittest discover -s tests -v
```

The project is distributed as source and local build artifacts; these commands do not assume a package has been published to PyPI.

### A useful change

- State the concrete input/problem and resulting behavior.
- Keep cloud transport, retained objects, conversion, verification, and offline recovery separate.
- Add a synthetic regression for a discovered failure, including interrupted or older-cache cases when relevant.
- Preserve original bytes and explicit failure/partial states.
- Update equivalent English and Chinese documentation when behavior or flags change.
- Do not add live credentials or network-dependent tests to CI.
- Do not upload user data, add telemetry, propagate remote deletions, or bypass account security.
- Reference primary protocol evidence for a new endpoint or format; do not copy upstream code or assets under an incompatible license.

### Reproducers

Use a small generated fixture. Describe Python/OS/tool version, a redacted error code, and exact commands. Do not attach a real backup, auth JSON, HAR, certificate, signed URL, or note screenshot.

Security findings belong in the [security reporting process](SECURITY.md), not a public issue containing exploitable private details.

### Before a release

1. Run the synthetic test suite.
2. Run the offline demo and recovery drill in new directories.
3. Check source and built package contents.
4. Review tracked files and Git history for private data and credential-like material.
5. Verify command examples, relative links, archive-format notes, and bilingual consistency.
6. Publish software and synthetic examples only.

These gates provide evidence for the tested scope. They do not justify claiming every user's historical data can be recovered.

Contributions are made under this repository's Apache-2.0 license. Referenced third-party projects retain their own licenses.

## 简体中文

先阅读[架构](docs/ARCHITECTURE.zh-CN.md)、[档案格式](docs/FORMAT.zh-CN.md)、[安全边界](docs/SECURITY.zh-CN.md)。

### 开发环境

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.lock
python -m pip install --no-deps -e .
python -m unittest discover -s tests -v
```

项目提供源码和本地构建产物，以上命令不假定 PyPI 已发布同名包。

### 有效的改动

- 说明具体输入/问题和改变后的行为。
- 云端传输、原件保留、转换、校验、离线恢复分层维护。
- 为真实发现的故障补合成回归，必要时覆盖中断或旧缓存。
- 保留原始字节，明确失败/部分完成。
- 行为或参数变化时同步中英双语文档。
- 不向 CI 添加真实凭据或必须联网的测试。
- 不上传用户数据、不加遥测、不传播远端删除、不绕过账号安全。
- 新接口或格式注明主源证据，不复制许可证不兼容的上游代码或资产。

### 复现方式

使用小型生成样例，说明 Python/OS/工具版本、脱敏错误码、准确命令。不附真实备份、auth JSON、HAR、证书、签名 URL、笔记截图。

安全问题按[漏洞报告流程](SECURITY.md)处理，不在公开 issue 放置可利用的私密细节。

### 发布前

1. 跑合成测试。
2. 在新目录完成离线 demo 和恢复演练。
3. 检查源码与构建包内容。
4. 审查跟踪文件和 Git 历史中的私人资料、凭据特征。
5. 核对命令、相对链接、档案格式说明、中英一致性。
6. 只发布软件与合成样例。

检查证明的是已测范围，不证明每个人的全部历史数据都能取回。

贡献按本仓库 Apache-2.0 许可证提供。引用第三方项目继续保留各自许可证。
