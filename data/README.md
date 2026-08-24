# 运行数据目录

`data/` 只用于本地运行数据，除本说明外均不纳入 Git。

- `group_essence.db`：默认 SQLite 数据库。
- `screenshots/`：等待 OCR 导入的 QQ 精华消息截图。
- `images/`：后续下载或生成的消息图片资源。

请勿在这里存放需要随源码发布的资源。可公开、已脱敏的测试样本应放在
`tests/fixtures/`，文档配图应放在 `docs/assets/`。令牌、账号配置等敏感信息只应
保存在仓库根目录的 `.env` 中。
