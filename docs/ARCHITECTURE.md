# 架构说明

## 目标

Group Essence Extractor 将不同来源的 QQ 群精华消息转换为统一记录，持久化到
SQLite，并通过 CLI 和 HTTP API 提供相同的搜索能力。项目以单机、小规模采集为
主要场景，优先保持部署简单和数据可追溯。

## 模块边界

| 模块 | 职责 |
| --- | --- |
| `config.py` | 从 `.env` 和环境变量构造不可变设置 |
| `fetchers.py` | 调用 OneBot、补全消息详情并标准化响应 |
| `ocr.py` | 调用 Tesseract 将单张图片转换为文本 |
| `parsers.py` | 从 OCR 文本提取结构化字段并生成截图指纹 |
| `models.py` | 定义来源无关的 `EssenceMessage` |
| `ingest.py` | 选择来源、执行回退并汇总写入统计 |
| `db.py` | 初始化 SQLite、更新或插入记录、执行搜索 |
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

### OCR 回退

当 OneBot 返回空列表、请求失败，或明确关闭 OneBot 时，只要
`FALLBACK_OCR=true`，程序就会扫描 `SCREENSHOT_DIR` 下的 PNG、JPEG 和 WebP。

每张截图先执行全文 OCR，再通过标签和日期规则提取发送者、发送时间、精华时间
和设置人。截图内容的 SHA-256 作为稳定消息 ID，使同一文件重复导入时可以更新
原记录。单张图片失败不会中断其他图片，最终通过 `ocr_error_count` 报告数量。

## 数据身份与写入

OneBot 记录优先通过 `(source, group_id, message_id)` 查找已有行，并兼容更新旧版
写入但缺少 `group_id` 的记录。OCR 记录通过内容指纹查找，同时兼容按截图路径匹配
旧记录。

写入结果分为：

- `inserted`：新增记录；
- `updated`：稳定身份相同但字段或原始响应发生变化；
- `unchanged`：记录内容完全一致，或旧复合唯一约束判定为重复。

`raw_json` 保存采集源的原始结构和补全响应，业务搜索只读取标准化列。数据库初始化
使用幂等 DDL，现有数据库可在下次正常启动时补建索引。

## API 生命周期

`create_app` 支持传入设置和仓库实例，便于测试和嵌入。默认 `app` 在 ASGI 生命周期
启动阶段初始化数据库，因此单纯导入 `group_essence_extractor.api` 不会写数据库。
运行中的设置和仓库存放在 `app.state`，路由不依赖模块级可变全局对象。

## 文件边界

- `src/`、`tests/`、`docs/`、`.github/` 和配置模板属于源码仓库。
- `data/` 中的数据库、截图和下载资源属于本地运行数据。
- 自动测试使用系统临时目录，不读写默认数据库，也不依赖真实 OneBot 或
  Tesseract 服务。
- 可提交测试资源必须体积小、内容公开且完成脱敏。

## 后续演进

- 对 OneBot 图片地址执行受控下载和单图 OCR。
- 使用 UI 区域分割提升截图字段识别准确率。
- 数据量增长后引入 SQLite FTS5 与明确的数据库迁移版本。
- 在需要远程部署时增加鉴权、速率限制和结构化日志。
