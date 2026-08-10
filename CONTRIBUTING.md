# Contributing

感谢你改进 SenseNova Present WebUI。

## 开发准备

1. Fork 并克隆仓库。
2. 复制 `.env.example` 为 `.env`，不要提交真实密钥。
3. 使用 Python 3.12 执行 `./start.sh --check` 完成依赖准备。
4. 使用 `./start.sh --reload` 启动开发服务。

## 提交前检查

```bash
./start.sh --check
cd studio
.venv/bin/python -m unittest discover -s tests
```

前端 JavaScript 修改还应执行：

```bash
node --check studio/static/app.js
```

提交内容应保持聚焦，并说明用户影响、验证方法及兼容性变化。请勿提交 `.env`、数据库、上传附件、生成工作区、模型密钥或私有服务地址。

## Issue 建议

报告问题时请包含操作系统、Python 版本、启动方式、脱敏后的日志、复现步骤以及预期/实际结果。涉及上传材料时请先移除敏感信息。
