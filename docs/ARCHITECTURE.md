# 架构说明

## 目标

Group Essence Extractor 将不同来源的 QQ 群精华消息转换为统一记录，持久化到
SQLite，并通过 CLI 和 HTTP API 提供相同的搜索能力。项目以单机、小规模采集为
主要场景，优先保持部署简单和数据可追溯。

## 模块边界

| 模块 | 职责 |
| --- | --- |
| `config.py` | 从 `.env` 和环境变量构造不可变设置 |
| `diagnostics.py` | 以只读方式检查本机配置和运行条件 |
| `fetchers.py` | 调用 OneBot、补全消息详情并标准化响应 |
| `ocr.py` | 调用 Tesseract 将单张图片转换为文本 |
| `parsers.py` | 从 OCR 文本提取结构化字段并生成截图指纹 |
| `models.py` | 定义来源无关的 `EssenceMessage` |
| `ingest.py` | 选择来源、执行回退并汇总写入统计 |
| `image_enrichment.py` | 发现 OneBot 图片、哈希缓存、OCR 与重试 |
| `db.py` | 迁移 SQLite、更新或插入记录、审计、修复和分页搜索 |
| `exporters.py` | 将分页搜索结果写为稳定结构的 JSON 或 CSV |
| `cli.py` | 命令行参数与输出适配 |
| `api.py` | FastAPI 应用工厂、生命周期和 HTTP 适配 |

CLI 和 API 只负责输入输出，采集规则集中在 `ingest.py`，存储规则集中在
`db.py`。

## 采集流程

### OneBot

1. `ingest_all` 在 `PREFER_ONEBOT=true` 时创建 `OneBotClient`。
2. 客户端调用 `get_essence_msg_list`，`GROUP_ID` 是必填参数。
3. 若精华项缺少 `sender_time` 或正文，客户端按 `message_id` 调用 `get_msg`。
4. 时间戳统一为本地时间 `YYYY-MM-DD HH:MM:SS`；秒、毫秒、微秒和纳秒输入均可
   归一化。
5. 纯文本、纯图片和混合图文分别标记为 `text`、`image` 和 `mixed`。多个图片
   地址以换行分隔保存在 `image_path`。
6. 单条详情补全失败只记录在 `raw_data` 中，不丢弃已经取得的精华项；精华列表
   请求失败则交由上层决定是否 OCR 回退。

采集完成后统一生成字段质量摘要。`ingest --dry-run` 到此结束，不创建仓库实例；
正式采集才进入数据库更新步骤。

### OCR 回退

当 OneBot 返回空列表、请求失败，或明确关闭 OneBot 时，只要
`FALLBACK_OCR=true`，程序就会扫描 `SCREENSHOT_DIR` 下的 PNG、JPEG 和 WebP。

每张截图先执行全文 OCR，再通过标签和日期规则提取发送者、发送时间、精华时间
和设置人。截图内容的 SHA-256 作为稳定消息 ID，使同一文件重复导入时可以更新
原记录。单张图片失败不会中断其他图片，最终通过 `ocr_error_count` 报告数量。

### OneBot 图片补全

图片补全与消息采集分为两个显式步骤，避免普通 `ingest` 隐式产生大量网络请求和
缓存文件。`enrich-images` 默认只读扫描 `image_path` 中的 HTTP(S) 地址，并对照
附件表计算已完成和待处理数量；预览不联网、不迁移数据库、不创建 `IMAGE_DIR`。

`--apply` 模式逐个流式读取受大小限制的图片，在内存中校验图片格式并计算 SHA-256，
再写入 `<哈希前缀>/<哈希>.<格式>`。相同内容只对应一个物理文件，多条附件记录可
引用同一路径。附件 OCR 成功后按图片顺序聚合回消息的 `ocr_text`，并与原正文共同
生成 `content_search`。

附件状态为 `completed`、`no_text` 或 `failed`。前两者重复执行时跳过；失败记录
保留下载成功后的缓存信息，下次可以跳过网络请求直接重试 OCR。单条失败只增加聚合
计数，不中断其他图片，命令输出不暴露远端地址或识别正文。

## 数据身份与写入

OneBot 记录优先通过 `(source, group_id, message_id)` 查找已有行，并兼容更新旧版
写入但缺少 `group_id` 的记录。OCR 记录通过内容指纹查找，同时兼容按截图路径匹配
旧记录。

写入结果分为：

- `inserted`：新增记录；
- `updated`：稳定身份相同但字段或原始响应发生变化；
- `unchanged`：记录内容完全一致，或旧复合唯一约束判定为重复。

`raw_json` 保存采集源的原始结构和补全响应，业务搜索只读取标准化列。数据库以
SQLite `PRAGMA user_version` 标记结构版本；迁移按版本顺序在事务中执行，重复初始化
不会重放迁移，版本高于程序支持范围时拒绝打开，避免旧程序误写新结构。

schema v2 新增 `essence_attachments`，以 `essence_id` 关联消息，并保存图片位置、
远端地址、本地相对路径、内容哈希、MIME、字节数、OCR 文本、状态和错误。原始
`image_path` 保持不变，用于追溯来源；附件 OCR 通过消息表的现有字段参与搜索和导出。

`audit-db` 使用 SQLite `mode=ro` 连接，执行 `PRAGMA quick_check` 并聚合缺失字段、
重复稳定身份、来源/类型分布和时间范围。数据库文件不存在或表结构不正确时只返回
错误，不创建目录或空数据库；报告同时给出当前与支持的 schema 版本。

`repair-db` 默认同样使用只读连接。它只补充当前为空且能从 `raw_json` 确定恢复的
群号、消息 ID 和时间字段，并重新计算 `content_search`；预览只输出聚合计数，只有
显式 `--apply` 才在单个事务中写入。无法确定的值不猜测，保留原状并计入
`unresolved`。

搜索条件由仓库层统一组装，CLI、HTTP API 和导出器共享包含匹配、精确匹配及时间
闭区间语义。`search_page` 在同一筛选条件下分别读取总数和当前页；旧的 `search`
接口继续返回当前页列表。导出器以固定批次分页读取，JSON 保留分页总数和导出数，
CSV 使用稳定列顺序。

## API 生命周期

`create_app` 支持传入设置和仓库实例，便于测试和嵌入。默认 `app` 在 ASGI 生命周期
启动阶段初始化数据库，因此单纯导入 `group_essence_extractor.api` 不会写数据库。
运行中的设置和仓库存放在 `app.state`，路由不依赖模块级可变全局对象。

## 文件边界

- `src/`、`tests/`、`docs/`、`.github/` 和配置模板属于源码仓库。
- `data/` 中的数据库、截图和哈希图片缓存属于本地运行数据。
- 自动测试使用系统临时目录，不读写默认数据库，也不依赖真实 OneBot 或
  Tesseract 服务。
- 用户生成的 JSON/CSV 导出文件属于 `data/exports/` 下的本地运行数据。
- OneBot HTTP 契约测试仅启动回环地址上的临时服务，并读取脱敏 JSON 夹具。
- 可提交测试资源必须体积小、内容公开且完成脱敏。

## 后续演进

- 为需要 Cookie 或短期签名的图片源增加可插拔认证适配。
- 使用 UI 区域分割提升截图字段识别准确率。
- 数据量增长后引入 SQLite FTS5，并通过现有 schema 迁移机制升级索引。
- 在需要远程部署时增加鉴权、速率限制和结构化日志。
