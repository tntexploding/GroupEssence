# 运行数据目录

`data/` 只用于本地运行数据，除本说明外均不纳入 Git。

- `group_essence.db`：默认 SQLite 数据库。
- `screenshots/`：等待 OCR 导入的 QQ 精华消息截图；可先用 `ocr-preview` 做只读预检。
- `images/`：OneBot 图片的 SHA-256 去重缓存，由 `enrich-images --apply` 管理。
- `exports/`：通过 `essence export` 生成的 JSON/CSV 文件。

请勿在这里存放需要随源码发布的资源。可公开、已脱敏的测试样本应放在
`tests/fixtures/`，文档配图应放在 `docs/assets/`。令牌、账号配置等敏感信息只应
保存在仓库根目录的 `.env` 中。

`images/` 中的两级相对路径会被附件表引用，不要手工重命名单个文件。清理缓存前应
先确认数据库附件状态；Git 会忽略整个运行数据目录。
