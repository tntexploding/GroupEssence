# AstrBot 插件适配改写说明

本文说明如何将 GroupEssence 改造成“可复用核心 + AstrBot 薄适配层”，并通过云端
现有 AstrBot ↔ NapCat OneBot 链路完成真实环境验收。实现时应同时参考
[架构说明](./ARCHITECTURE.md) 和 [AstrBot 远端部署指南](./ASTRBOT_DEPLOYMENT.md)。

> 2026-08-25 阶段 A 实测修订：NapCat 的精华列表可能缺少历史发送时间，而旧消息
> 无法再由 `get_msg` 回查。因此详情请求只由正文缺失触发；发送时间缺失保留为质量
> 字段，不使用精华时间代填。验收详情请求另设数量上限。

## 1. 决策与目标

采用以下形态：

```text
QQ
└─ NapCat
   └─ AstrBot 的现有 OneBot 连接
      └─ GroupEssence AstrBot 插件
         ├─ 权限、命令、回复
         ├─ AstrBot OneBot Action 适配器
         └─ GroupEssence 核心
            ├─ 响应标准化
            ├─ SQLite/迁移/幂等写入
            └─ 搜索
```

短期目标：

- 不新增 NapCat HTTP 监听端口，不在插件中保存 NapCat Access Token；
- 由管理员通过 QQ 指令触发 `get_essence_msg_list`，先完成只读契约验收；
- 验收输出只包含计数、字段类型和质量统计，不包含消息正文或图片 URL；
- 验收通过后启用 SQLite 同步和 QQ 查询；
- 所有插件指令均停止事件传播，不进入 AstrBot 的 LLM 流程。

长期目标：

- GroupEssence 的模型、标准化、存储和搜索继续独立于 AstrBot；
- CLI、HTTP、AstrBot 可以复用同一核心，而不是维护三套解析逻辑；
- AstrBot 插件仅承担平台接入、权限、并发控制和消息呈递；
- 只有出现 AstrBot 以外的远程调用方时，才考虑部署独立服务容器。

本轮非目标：

- 不部署 GroupEssence 独立 HTTP 服务；
- 不把 FastAPI/Uvicorn 带入 AstrBot 插件运行路径；
- 不在第一轮启用截图 OCR、图片下载或 Tesseract；
- 不自动监听全部群消息，所有采集只由明确的“精华”指令触发；
- 不允许普通用户传入任意群号进行跨群查询。

## 2. 保留与改写边界

### 2.1 直接保留

以下内容应继续作为框架无关核心：

- `models.py` 中的 `EssenceMessage`；
- `db.py` 中的迁移、`EssenceRepository`、`SaveStats` 和 `SearchPage`；
- `image_enrichment.py`、`ocr.py` 和 `parsers.py`，但第一版插件不导入、不启用；
- `exporters.py`、CLI 和 HTTP API，继续服务于离线维护；
- 当前数据库稳定身份、幂等更新和 schema 版本规则。

### 2.2 必须拆分

当前 `fetchers.OneBotClient` 同时承担：

1. HTTP 请求；
2. OneBot 响应检查；
3. 是否需要 `get_msg` 的判断；
4. 消息字段标准化。

插件不能复用其中的同步 HTTP 请求，但必须复用标准化规则。因此应把纯逻辑移动到
新模块，例如 `normalization.py`：

```python
def needs_message_detail(item: Mapping[str, Any]) -> bool: ...

def get_message_id(item: Mapping[str, Any]) -> str: ...

def normalize_essence_item(
    item: Mapping[str, Any],
    *,
    requested_group_id: str,
    detail: Mapping[str, Any] | None = None,
    detail_error: str = "",
) -> EssenceMessage: ...

def normalize_essence_items(
    items: Iterable[Mapping[str, Any]],
    *,
    requested_group_id: str,
    details: Mapping[str, Mapping[str, Any]] | None = None,
    detail_errors: Mapping[str, str] | None = None,
) -> list[EssenceMessage]: ...
```

`needs_message_detail` 只判断正文是否缺失。仅缺少 `sender_time` 时必须返回 false，
避免对已经不在 NapCat 回查记录中的历史消息逐条请求。

`_fmt_ts`、`_parse_message_content`、`_pick_id` 和 `_first_value` 也应移入该模块。
它不得导入 `requests`、FastAPI、AstrBot、Pillow 或 pytesseract。

改写后的 `OneBotClient` 仍使用 `requests`，但只负责取得 list/detail，再调用上述
纯函数。这样原 CLI 行为和原测试夹具仍然有效。

### 2.3 新增 AstrBot Action 适配器

新增一个异步适配器，例如 `astrbot_source.py`。它负责从当前消息事件取得
`event.bot.api.call_action`，而不是连接 `ONEBOT_BASE_URL`。

建议接口：

```python
class AstrBotEssenceSource:
    async def get_essence_messages(
        self,
        event: AstrMessageEvent,
        group_id: str,
    ) -> list[EssenceMessage]:
        ...
```

处理流程：

1. 检查 `event.bot.api.call_action` 是否可调用；
2. 调用 `get_essence_msg_list(group_id=...)`；
3. 同时兼容“直接返回 data”和“返回完整 OneBot envelope”；
4. 校验最终 data 必须是 list；
5. 仅对缺少正文的项目调用 `get_msg`；验收模式受配置的请求上限约束；
6. 单条详情失败时保留精华项目，只设置脱敏后的 `detail_error`；
7. 调用 `normalization.py` 生成 `EssenceMessage`。

Action 解包不能假定固定返回形态：

```python
def unwrap_action_result(result: Any) -> Any:
    if isinstance(result, dict) and "data" in result:
        status = str(result.get("status", "")).lower()
        retcode = result.get("retcode")
        if (status and status != "ok") or retcode not in (None, 0):
            raise OneBotActionError.from_envelope(result)
        return result["data"]
    return result
```

`OneBotActionError` 对外只保留 action、status、retcode 和经过截断的 wording。异常、
日志和 QQ 回复中都不得包含 payload、Token、Cookie、原始响应、消息正文或图片 URL。

## 3. 插件工程布局

为了让 AstrBot 能直接从 Git 仓库安装，最终插件入口应位于仓库根目录：

```text
GroupEssence/
├─ main.py                         # AstrBot 插件入口，只做编排
├─ metadata.yaml
├─ _conf_schema.json
├─ requirements.txt                # 仅列插件运行必需依赖
├─ src/
│  ├─ __init__.py                  # 允许插件入口使用包内相对导入
│  └─ group_essence_extractor/
│     ├─ astrbot_source.py          # 新增：异步 Action 适配
│     ├─ normalization.py           # 新增：纯标准化逻辑
│     ├─ plugin_service.py          # 新增：同步/查询用例
│     ├─ models.py
│     ├─ db.py
│     └─ ...
├─ tests/
└─ docs/
```

不要在 `main.py` 中使用 `sys.path.insert`。入口应使用包内相对导入，或者在最终拆分
为独立插件仓库时把 GroupEssence 核心作为正常 Python 包安装。

AstrBot 会读取插件根目录的 `requirements.txt`。当前文件包含 FastAPI、Pydantic、
Uvicorn、Pillow 等完整应用依赖，直接让 AstrBot 安装这些固定版本可能与 AstrBot
自身依赖冲突。改写时应：

- 让插件运行路径只使用 Python 标准库和 AstrBot 已提供的 API；
- 将 CLI/HTTP/OCR 的完整开发安装改为 `pip install -e .` 或 pyproject extras；
- 不在插件 `requirements.txt` 中固定 AstrBot 自身已经使用的 FastAPI/Pydantic 版本；
- 第一版插件不安装 Tesseract，也不导入 OCR 模块。

如果暂时不调整仓库根布局，可以先生成一个完整插件目录 ZIP 上传测试，但 ZIP 中仍
必须同时包含入口及其所需核心代码，不能依赖云端源码仓库的偶然路径。

## 4. 插件配置

使用 `_conf_schema.json`，不要读取 GroupEssence 的 `.env`。建议至少提供：

| 配置 | 类型 | 初始值 | 说明 |
| --- | --- | --- | --- |
| `validation_mode` | bool | `true` | 为 true 时只允许验收，不写数据库 |
| `admin_ids` | list | `["2573423682"]` | 可以验收、同步和查询的 QQ 账号 |
| `allowed_group_ids` | list | `[]` | 必须显式填写，空列表拒绝所有目标群 |
| `default_group_id` | string | `""` | 私聊指令使用的默认目标群，且必须在白名单内 |
| `max_validation_detail_requests` | int | `10` | 验收阶段最多补全正文数量，代码限制为 0–50 |
| `max_query_results` | int | `5` | 单次最多返回数量，代码中再限制为 1–20 |
| `max_content_chars` | int | `300` | 单条正文最大呈递字符数 |
| `enable_image_enrichment` | bool | `false` | 第一版保持关闭 |
| `enable_scheduled_sync` | bool | `false` | 验收完成前保持关闭 |

不应出现：

- `ONEBOT_BASE_URL`；
- `ONEBOT_ACCESS_TOKEN`；
- 任意 NapCat WebUI 账号或 Token；
- 可以绕过 `allowed_group_ids` 的“允许任意群”开关。

目标群解析规则固定为：

1. 群聊中默认取 `event.get_group_id()`；
2. 私聊中使用 `default_group_id`；
3. 命令参数包含群号时，仅管理员可用；
4. 无论来源如何，最终群号必须存在于 `allowed_group_ids`；
5. 群号统一转为去除空白的字符串比较，调用 Action 时数字群号可转为 int。

## 5. 权限与事件传播

第一版所有命令仅允许 `admin_ids` 中的账号。即使 AstrBot 另有管理员配置，也要执行
插件自己的精确白名单检查，避免权限范围意外扩大。

每个命令处理器进入后立即调用 `event.stop_event()`，包括：

- 权限拒绝；
- 配置错误；
- OneBot Action 失败；
- 数据库失败；
- 正常成功。

示意骨架：

```python
@filter.command("精华验收")
@filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
async def validate_essence(self, event: AstrMessageEvent, group_id: str = ""):
    event.stop_event()

    target = self.resolve_authorized_group(event, group_id)
    if target is None:
        yield event.plain_result("无权限或目标群未在允许列表中。")
        return

    try:
        report = await self.service.validate(event, target)
    except SafePluginError as exc:
        yield event.plain_result(f"精华验收失败：{exc.public_message}")
        return

    yield event.plain_result(format_validation_report(report))
```

不要用全量消息监听器识别关键词“精华”，也不要在未匹配命令时调用
`stop_event()`，否则会干扰 AstrBot 的普通聊天功能。

## 6. 命令设计

### 6.1 第一阶段：只读验收

仅启用：

```text
/精华验收 [群号]
/精华状态
```

`/精华验收` 不初始化数据库、不创建数据目录、不写入原始响应。建议回复：

```text
精华验收成功
目标群：已授权
采集数量：12
内容类型：text=9, image=2, mixed=1
缺失字段：message_id=0, sender_time=0, essence_time=0, content=0
详情补全：候选=3, 请求=3, 跳过=0, 失败=0
```

不得回复群号、QQ 号、昵称、正文、图片 URL 或完整 Action 结果。目标群只显示
“已授权”，错误消息只显示 action/status/retcode。

### 6.2 第二阶段：写库和查询

`validation_mode=false` 后启用：

```text
/精华同步 [群号]
/精华查询 <关键词>
/精华最近 [数量]
/精华状态
```

行为约束：

- `精华同步` 先取得并标准化全部项目，再在单个受控数据库操作中 upsert；
- `精华查询` 必须限定目标群，不能跨白名单群返回结果；
- 空关键词拒绝执行，不允许用空字符串导出整个数据库；
- `精华最近` 的数量限制在 1–20；
- QQ 回复不包含 `raw_json`、`remote_url`、本地绝对路径或数据库路径；
- 正文按 `max_content_chars` 截断，结果过多时只提示剩余条数；
- 回复使用 `yield event.plain_result(...)`，不要再次调用 OneBot 发送消息 Action。

建议的单条结果格式：

```text
[1/3] 2026-08-24 20:15
发送者：显示名（QQ 号默认不展示）
内容：截断后的正文
类型：text
```

## 7. 服务层与并发

新增 `plugin_service.py`，使 `main.py` 不直接操作仓库。建议职责：

```python
class GroupEssencePluginService:
    async def validate(self, event, group_id: str) -> ValidationReport: ...
    async def sync(self, event, group_id: str) -> SyncReport: ...
    async def search(self, group_id: str, keyword: str, limit: int) -> SearchPage: ...
    async def recent(self, group_id: str, limit: int) -> SearchPage: ...
    async def status(self) -> StatusReport: ...
```

`EssenceRepository` 是同步 SQLite API。插件不得直接在事件循环中执行迁移、upsert、
搜索或审计，应使用 `asyncio.to_thread`。同时用一个 `asyncio.Lock` 串行化初始化和
数据库写入：

```python
self.operation_lock = asyncio.Lock()

async with self.operation_lock:
    await asyncio.to_thread(self.repository.init_db)
    stats = await asyncio.to_thread(self.repository.upsert_messages, messages)
```

建议同步命令也共用该锁，防止多个管理员命令同时请求全量精华。查询可以先共用同一
锁保证简单可靠，确认负载后再拆分读写锁。不要在 `__init__` 中做同步迁移或创建
后台任务；数据库可在第一次需要写入时惰性初始化。

`validate` 应把 `max_validation_detail_requests` 作为仅本次验收的上限传给 Action
适配器；`sync` 不复用该上限。报告必须区分正文缺失候选数、实际详情请求数、跳过数
和失败数。

## 8. 持久化目录

数据库和附件必须位于 AstrBot 数据卷：

```python
from pathlib import Path
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

data_dir = (
    Path(get_astrbot_data_path())
    / "plugin_data"
    / "astrbot_plugin_group_essence"
)
db_path = data_dir / "group_essence.db"
image_dir = data_dir / "images"
```

禁止写入：

- 插件源码目录；
- 容器临时目录；
- 当前工作目录推导出的 `./data`；
- GroupEssence Git 仓库中的 `data/`。

部署更新前只需保护 AstrBot 的数据卷；禁用或替换插件时不得删除上述目录。数据库
升级继续使用现有 `PRAGMA user_version` 迁移，不通过手工 SQL 修改生产数据库。

## 9. 日志与隐私

使用 `from astrbot.api import logger`。允许记录：

- 命令名称；
- 脱敏后的调用者标识，例如 QQ 号哈希或“管理员”；
- action 名称、耗时、status、retcode；
- 采集/新增/更新/未变化数量；
- 字段缺失计数和异常类别。

禁止记录：

- Access Token、Cookie、Authorization 请求头；
- 完整 QQ 号、群号和昵称；
- 消息正文、OCR 正文；
- 原始 OneBot JSON；
- 图片 URL、签名参数或本地绝对路径；
- `event.message_str` 全文。

`raw_data` 可以继续写入本地 SQLite 用于追溯，但不得进入日志或 QQ 验收回复。若未来
需要更严格的数据最小化，再增加配置决定是否持久化原始响应；不要在本轮顺便改变
现有数据库语义。

## 10. 测试改写

### 10.1 保持现有测试

现有测试必须继续通过。特别关注：

- `test_fetchers.py`；
- `test_onebot_contract.py`；
- `test_ingest.py`；
- `test_repository.py`。

抽离 `normalization.py` 后，应先迁移测试而不是修改期望结果，确保 HTTP CLI 行为
没有回归。

### 10.2 新增核心测试

至少覆盖：

- AstrBot Action 返回完整 envelope；
- AstrBot Action 直接返回 list/dict；
- status 非 ok 或 retcode 非 0；
- 精华 data 不是 list；
- 缺正文时调用 `get_msg`；
- 正文存在但 `sender_time` 缺失时不调用 `get_msg`，且发送时间保持空值；
- 验收详情请求达到上限后不再调用 `get_msg`；
- 单条 `get_msg` 失败仍保留记录；
- 时间戳、图文段、发送者和设置人标准化与原 HTTP 客户端一致；
- 响应错误不会把原始 payload 写进公开异常。

Action 测试使用 fake async `call_action`，不连接 NapCat。

### 10.3 新增插件测试

至少覆盖：

- 非 `admin_ids` 调用被拒绝；
- 群号不在白名单时被拒绝；
- 私聊缺少 `default_group_id` 时被拒绝；
- 每条命令无论成功或失败都会调用 `stop_event()`；
- `validation_mode=true` 时不会创建数据库；
- 同步两次时第二次主要为 `unchanged`；
- 查询强制附带 `group_id`；
- 输出不包含 raw_json、URL、Token 和绝对路径；
- SQLite 操作通过线程执行，并发同步不会重入。

AstrBot 入口测试可在 AstrBot 开发环境中运行；核心测试环境不要为了导入
`main.py` 而安装整个 AstrBot。

## 11. 云端验收顺序

### 阶段 A：安装但只读

1. 备份 AstrBot 数据卷；
2. 安装插件，保持 `validation_mode=true`；
3. 配置 `admin_ids=["2573423682"]`；
4. 只加入一个测试群到 `allowed_group_ids`；
5. 保持图片补全、OCR、计划任务关闭；
6. 私聊机器人执行 `/精华验收`；
7. 检查 AstrBot 和 NapCat CPU/内存没有持续上升；
8. 检查日志中没有正文、URL、QQ 号和 Token。

阶段 A 通过标准：

- `get_essence_msg_list` 成功；
- 返回数量符合群内实际情况；
- `message_id`、发送时间、精华时间和正文缺失数被准确报告；
- `get_msg` 补全失败不会中断整批；
- 普通聊天仍按原 AstrBot 规则处理；
- 验收命令不会进入 LLM。

发送时间缺失是否允许进入阶段 B 由业务门禁决定。若必须恢复历史发送时间，应另行
实现群历史消息分页，并按群号和 `message_id` 匹配；禁止用 `essence_time` 推断。

### 阶段 B：启用写库

1. 设置 `validation_mode=false`；
2. 执行一次 `/精华同步`；
3. 立即再次执行相同命令；
4. 第二次在无新数据时应主要为 `unchanged`，不得重复新增；
5. 用脱敏关键字执行 `/精华查询`；
6. 重启 AstrBot 容器后再次查询，确认数据仍存在。

### 阶段 C：有限生产使用

1. 保持管理员专用至少一个观察周期；
2. 观察命令耗时、Action 错误和数据库大小；
3. 再决定是否开放只读查询用户；
4. 最后才评估定时同步和图片 OCR。

## 12. 回滚

插件必须支持无损回滚：

1. 在 AstrBot WebUI 禁用插件；
2. 确认普通 QQ 对话和其他插件恢复正常；
3. 保留 `data/plugin_data/astrbot_plugin_group_essence/`；
4. 回退插件代码版本；
5. 只有确认旧版本支持当前 `PRAGMA user_version` 后才重新启用。

不要通过删除数据库解决代码兼容问题。若新版迁移已提高 schema 版本，应提供向前
兼容修复版本，而不是让旧代码强行打开新库。

## 13. 完成定义

只有同时满足以下条件，才认为插件改写完成：

- 核心标准化逻辑不依赖 AstrBot 或 HTTP transport；
- 原有全部自动测试通过，新增 Action/权限/传播测试通过；
- 插件不需要 NapCat URL 或 Token；
- 插件只访问白名单群，第一版只允许管理员账号；
- 验收模式完全只读；
- 同步幂等，重启后数据保留；
- 查询结果能够由 QQ 收到且不会继续进入 LLM；
- 不新增公网端口；
- 日志、异常和验收输出满足脱敏要求；
- 禁用插件即可回滚，不影响 AstrBot、NapCat、LAS 和 ConnectionDB。

建议按三个独立提交实施：

1. `refactor: extract transport-independent essence normalization`；
2. `feat: add validation-only AstrBot adapter`；
3. `feat: enable guarded persistence and query commands`。

每个提交都应保持测试可运行，不要把核心重构、云端部署和 OCR 镜像修改合并在一次
变更中。
