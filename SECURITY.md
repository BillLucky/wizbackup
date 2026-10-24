# Reporting a security issue / 安全问题报告

## English

Report vulnerabilities in the software without publishing private archive contents.

If GitHub private vulnerability reporting is enabled for this repository, use its [private reporting form](https://github.com/BillLucky/wizbackup/security/advisories/new). If that route is unavailable, open a public issue asking for a private contact channel **without vulnerability details or sensitive data**.

A useful private report includes:

- affected tool version and operating system;
- expected and observed behavior;
- a small synthetic reproducer;
- the affected boundary: path extraction, credentials, network routing, cache writes, or content rendering;
- impact and any mitigation already tested.

Never send a real token, password, certificate, note body, client database, backup ZIP, HAR, or signed resource URL. Use generated material that demonstrates the failure. Avoid testing another person's account or a production endpoint beyond your authorization.

There is no guaranteed response time. Maintainers may request a reduced synthetic fixture. Public disclosure should avoid exposing users' private material.

See [the security model](docs/SECURITY.md) for supported boundaries and known limitations. Ordinary download failures belong in normal issues after redaction.

## 简体中文

报告软件漏洞时不要公开私人档案内容。

如果本仓库启用了 GitHub 私密漏洞报告，请使用[私密报告表单](https://github.com/BillLucky/wizbackup/security/advisories/new)。若该入口不可用，可在公开 issue 中仅询问私密联系渠道，**不附漏洞细节或敏感数据**。

有用的私密报告包括：

- 受影响版本和操作系统；
- 预期与实际行为；
- 小型合成复现；
- 相关边界：路径提取、凭据、网络路由、缓存写入、内容渲染；
- 影响及已测试的缓解方式。

不要发送真实 token、密码、证书、正文、客户端数据库、ZIP 备份、HAR 或签名资源 URL。使用生成资料复现，不对他人账号或生产接口进行超出授权的测试。

不承诺固定响应时限，维护者可能要求缩小合成样例。公开披露不应暴露用户私人资料。

[安全模型](docs/SECURITY.zh-CN.md)列出了能力边界和已知限制。普通下载失败在脱敏后走一般 issue。
