# 架构说明

## 目标

Group Essence Extractor 将不同来源的 QQ 群精华消息转换为统一记录，持久化到
SQLite，并通过 AstrBot 插件、CLI 和 HTTP API 复用相同的标准化与搜索能力。远端
首选 AstrBot 薄适配层；独立应用继续服务于离线维护和无需 AstrBot 的场景。

## 模块边界

| 模块 | 职责 |
| --- | --- |
| `config.py` | 从 `.env` 和环境变量构造不可变设置 |
| `diagnostics.py` | 以只读方式检查本机配置和运行条件 |
| `normalization.py` | 与传输无关地判断详情需求、解析消息段并生成 `EssenceMessage` |
| `quality.py` | 汇总标准化记录的字段缺失、内容类型与 OCR 质量 |
| `fetchers.py` | 独立应用的同步 HTTP OneBot 请求与详情取得 |
| `astrbot_source.py` | 通过事件或后台网关异步调用 OneBot Action |
| `astrbot_gateway.py` | 按平台 ID 动态解析 AIOCQHTTP 客户端，不持有消息事件 |
| `plugin_config.py` | 解析插件配置并执行管理员、群白名单授权 |
| `plugin_service.py` | 编排只读验收、同步、详情重试、查询、状态和并发控制 |
| `runtime.py` | 管理单实例后台任务、超时退避、告警、备份和健康快照 |
| `ocr.py` | 调用 Tesseract，自适应选择原图或低置信度灰度兜底结果 |
| `parsers.py` | 从标签模板或 QQ 卡片布局提取字段并生成截图指纹 |
| `models.py` | 定义来源无关的 `EssenceMessage` 与最小时间匹配记录 |
| `ingest.py` | 选择来源、执行回退并汇总写入统计 |
| `image_enrichment.py` | 发现 OneBot 图片、哈希缓存、OCR 与重试 |
| `db.py` | 迁移 SQLite、更新或插入记录、审计、修复和分页搜索 |
| `exporters.py` | 将分页搜索结果写为稳定结构的 JSON 或 CSV |
| `cli.py` | 命令行参数与输出适配 |
| `api.py` | FastAPI 应用工厂、生命周期和 HTTP 适配 |
| 根目录 `main.py` | AstrBot 指令、事件拦截、脱敏回复与日志适配 |

CLI、API 和 AstrBot 入口只负责平台输入输出。HTTP 和 AstrBot 分别取得原始数据后
都调用 `normalization.py`；存储规则集中在 `db.py`。

## 采集流程

### OneBot

1. 独立应用的 `ingest_all` 在 `PREFER_ONEBOT=true` 时创建 `OneBotClient`。
2. HTTP 客户端调用 `get_essence_msg_list`，`GROUP_ID` 是必填参数。
3. 若精华项缺少正文，HTTP 客户端按 `message_id` 调用 `get_msg`；仅缺
   `sender_time` 不触发详情请求。
4. 时间戳统一为本地时间 `YYYY-MM-DD HH:MM:SS`；秒、毫秒、微秒和纳秒输入均可
   归一化。
5. 纯文本、纯图片和混合图文分别标记为 `text`、`image` 和 `mixed`。多个图片
   地址以换行分隔保存在 `image_path`。
6. `fetchers.py` 和 `astrbot_source.py` 将结果交给同一组纯标准化函数，保证两种
   transport 的字段语义一致。
7. 单条详情补全失败只记录在 `raw_data` 中，不丢弃已经取得的精华项；精华列表
   请求失败则交由上层决定是否 OCR 回退。

采集完成后统一生成字段质量摘要。`ingest --dry-run` 到此结束，不创建仓库实例；
正式采集才进入数据库更新步骤。

### AstrBot Action 适配

手动指令与后台同步在 Action 层汇合：

```text
QQ 管理员指令 -> AstrBot AIOCQHTTP 事件 -> event.bot 的 call_action
AstrBot 生命周期 -> runtime.py -> Context.get_platform_inst(platform_id)
                                -> AIOCQHTTP get_client().call_action
  -> get_essence_msg_list / 正文缺失时 get_msg
  -> 新记录缺时间时一次有界 get_group_msg_history
  -> normalization.py
  -> plugin_service.py
  -> AstrBot 数据卷中的 SQLite
```

后台网关每次调用都按显式平台 ID 重新取得当前客户端，因此适配平台重连，但不保存
事件或客户端实例。插件不连接 NapCat HTTP 地址，也不保存 Token。`astrbot_source.py` 同时兼容 Action
直接返回 data 与完整 OneBot envelope；status/retcode 异常只形成不含 payload 的
公开错误。单条详情失败保留精华项，并只记录脱敏后的错误摘要；同步请求另有上限，
schema v3 为失败 ID 保存独立的下次重试截止时间，按失败次数指数退避；到期后会再次
尝试，因此不会每轮重复请求，也不会永久跳过。仅缺发送时间不会触发 `get_msg`，也不
使用精华设置时间伪造发送时间。新记录可用一次有界群历史查询补全，旧记录由管理员
显式运行补全命令；历史结果只转换成消息身份和时间，不保留正文或发送者。

插件不注册全量群消息监听器。采集只来自明确的管理员命令或显式启用的白名单计划
任务，避免改变普通消息的 AstrBot 唤醒与 LLM 流程；后台采集和告警均不调用 LLM。

所有指令进入处理器后立即 `stop_event()`，因此不会继续进入 LLM。插件自身再次执行
管理员 ID 和群白名单精确匹配；群聊查询固定使用当前群，私聊使用同时在白名单内的
默认群，只有管理员同步/验收指令可以显式指定白名单群。

`validation_mode=true` 时只有验收和状态可实际执行，初始化插件、验收和状态都不会
创建数据库，后台同步和自动备份也不会启动。关闭该模式后，手动同步才惰性初始化数据库，查询仍拒绝空关键词且每次限制
在 1–20 条。验收阶段只处理最多 `max_validation_detail_requests` 个正文缺失项；正式
同步使用独立的 `max_sync_detail_requests` 上限，群历史读取使用
`history_query_limit`。报告区分候选、实际请求、跳过和失败数，避免历史消息批量失败
刷屏。同步、查询和审计等同步
SQLite 工作全部通过 `asyncio.to_thread` 执行，
服务实例用一个 `asyncio.Lock` 串行化 Action 与数据库操作，避免多个管理员命令重入。

### 无人值守运行时

0.4.0 的 `GroupEssenceRuntime` 由 `on_astrbot_loaded` 启动并由插件 `terminate` 取消，
同一插件实例最多拥有一个任务。它按群白名单排序串行执行同步，单群使用硬超时；失败
采用指数退避，达到阈值后至少等待一个正常同步周期，相当于打开降频熔断。成功后的
正常间隔带有有界抖动，避免固定时刻请求。

每群的开始、完成、下次运行、连续失败、错误类别、聚合写入统计、耗时和告警状态均
保存在 `essence_sync_state`。重启时清除遗留的 `running` 标记，但保留未来退避截止时间
与失败计数。告警只在首次达到持续失败阈值和随后恢复时发送给管理员，内容只包含聚合
计数和错误类别；发送失败不会中断同步，也不会被误记为已送达，后续降频周期会重试。

后台任务同时可按独立开关执行 SQLite 在线备份。备份写入临时文件，执行
`PRAGMA quick_check` 后原子改名，并按每日与每周集合保留；现有数据库发生 schema
迁移前总会先创建不受轮换管理的快照。`ge_health.json` 也通过临时文件原子替换，只有
状态、时间、计数和错误类别，不包含群 ID、管理员 ID 或消息数据。所有新后台能力默认
关闭，缺少平台 ID、白名单或仍处于验收模式时不会启动计划同步。

插件数据目录固定为 AstrBot 数据根目录下的
`plugin_data/astrbot_plugin_group_essence/`。源码目录、本仓库 `data/` 和当前工作目录
都不是插件持久化位置。

### OCR 回退

当 OneBot 返回空列表、请求失败，或明确关闭 OneBot 时，只要
`FALLBACK_OCR=true`，程序就会扫描 `SCREENSHOT_DIR` 下的 PNG、JPEG 和 WebP。

每张截图先根据 EXIF 校正方向并识别原图。若没有识别到词，或有效词的平均置信度
低于阈值，只追加一次三倍灰度放大、自动对比度和深色背景反转兜底；原图已足够清晰
时不会产生额外 OCR 开销。两次结果通过置信度、词数和文本长度组成的质量分数择优。

解析器首先匹配“发送者/发送时间/精华时间/设置人”等显式标签；未出现标签时，再
识别 QQ 精华卡片常见的三段布局：元数据上一行作为发送者，元数据内前两个日期分别
作为发送和精华时间，“由……设置为精华”提取设置人，后续行作为正文。日期统一为
补零格式，保留原始 OCR 全文供追溯和搜索，并在 `raw_data` 中记录识别策略、平均
置信度、词数和解析策略。

截图内容的 SHA-256 作为稳定消息 ID，使同一文件重复导入时可以更新原记录。单张
图片失败不会中断其他图片，最终通过 `ocr_error_count` 报告数量。质量摘要把
“未知发送者/未知设置人”视为缺失，并聚合完整记录数及策略分布。

`ocr-preview` 直接复用上述扫描、识别、解析与质量汇总逻辑，但在创建仓库实例之前
结束。它不连接 OneBot、不创建数据库，且只输出聚合统计，不输出文件名或 OCR 内容。

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
- `updated`：稳定身份相同且业务字段发生变化；
- `refreshed`：只有原始响应或 OneBot 短期图片地址变化；
- `unchanged`：记录内容完全一致，或旧复合唯一约束判定为重复。

写入合并不会用上游空值抹掉已经修复的发送时间、OCR 或有效正文。时间补全只更新
空值，并在 `raw_json` 写入最小来源标记；群历史响应本身不会保存。`raw_json` 仍保存
精华采集源的原始结构和正文详情补全响应，业务搜索只读取标准化列。数据库以
SQLite `PRAGMA user_version` 标记结构版本；迁移按版本顺序在事务中执行，重复初始化
不会重放迁移，版本高于程序支持范围时拒绝打开，避免旧程序误写新结构。

schema v2 新增 `essence_attachments`，以 `essence_id` 关联消息，并保存图片位置、
远端地址、本地相对路径、内容哈希、MIME、字节数、OCR 文本、状态和错误。原始
`image_path` 保持不变，用于追溯来源；附件 OCR 通过消息表的现有字段参与搜索和导出。

schema v3 新增 `essence_sync_state` 与 `essence_detail_retry`。两张表只保存运行控制所需
的群/消息稳定标识、截止时间、聚合计数和脱敏错误类别，不复制正文或 OneBot payload。
SQLite 保持默认 rollback journal；连接统一设置有界 `busy_timeout`，不为当前单任务
写入模型启用 WAL。

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

独立 FastAPI 应用不属于 AstrBot 插件运行路径。当前远端部署不启动它，也不为插件
增加端口；只有出现 AstrBot 以外的调用方时才评估独立部署及其鉴权边界。

## 文件边界

- `src/`、`tests/`、`docs/`、`.github/` 和配置模板属于源码仓库。
- 根目录 `main.py`、`metadata.yaml`、`_conf_schema.json` 和 `requirements.txt` 属于
  AstrBot 可安装插件边界；插件依赖表不得引入独立 API/OCR 依赖。
- `data/` 中的数据库、截图和哈希图片缓存属于本地运行数据。
- 远端插件运行数据只进入 AstrBot 数据卷的专用 `plugin_data` 子目录，不回写 Git
  仓库或插件源码目录；数据库备份位于其 `backups/` 子目录，健康快照为
  `ge_health.json`。
- 自动测试使用系统临时目录，不读写默认数据库，也不依赖真实 OneBot 或
  Tesseract 服务。
- OCR 引擎测试使用临时生成图片和模拟的 Tesseract TSV 数据；解析测试只使用脱敏
  合成文本。真实截图只允许通过本地 `ocr-preview` 手工验收。
- 用户生成的 JSON/CSV 导出文件属于 `data/exports/` 下的本地运行数据。
- OneBot HTTP 契约测试仅启动回环地址上的临时服务，并读取脱敏 JSON 夹具。
- 可提交测试资源必须体积小、内容公开且完成脱敏。

## 后续演进

- 为需要 Cookie 或短期签名的图片源增加可插拔认证适配。
- 使用 UI 区域分割提升截图字段识别准确率。
- 数据量增长后引入 SQLite FTS5，并通过现有 schema 迁移机制升级索引。
- 如业务必须覆盖单次 `history_query_limit` 之外的历史发送时间，再增加带游标、硬上限
  与速率限制的显式分页；仍只按群号与稳定消息身份匹配，不从精华时间推断。
- 若未来部署独立 HTTP 服务，为该服务增加鉴权、速率限制和结构化日志；AstrBot
  插件继续复用平台权限与现有 OneBot 连接。
