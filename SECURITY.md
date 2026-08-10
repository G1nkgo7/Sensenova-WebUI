# Security

## Supported release

当前仅维护 V1 静态演示发布版。

## Reporting a vulnerability

请勿在公开 Issue 中粘贴 API Key、访问令牌、完整 `.env`、用户附件或私有模型地址。安全问题请通过 GitHub 仓库所有者的私密联系方式报告，并附上最小复现、影响范围和建议修复方式。

## Deployment boundary

- V1 是免登录、本地单用户模式，不应直接暴露到不受信任的公网。
- 对外提供服务时应置于具备 HTTPS、访问控制、限流和日志脱敏能力的反向代理之后。
- `.env`、`studio/data/`、数据库、上传附件与生成目录必须排除在版本控制和公开备份之外。
- 自定义模型、生图和搜索服务的密钥由部署者负责保管和轮换。
