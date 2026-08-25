# Group Essence Extractor

一个面向 QQ 群精华消息的采集与检索工具。当前推荐的远端部署形态是 AstrBot
插件：复用 AstrBot 与 NapCat 已有的 OneBot 连接，在 QQ 中完成只读验收、同步与
查询，不增加 HTTP 端口或 NapCat Token。独立 CLI/API/OCR 仍保留，用于本地采集、
离线维护和截图识别。

## 功能

- 可直接从仓库安装为 AstrBot 插件，提供 `/精华验收`、`/精华同步`、
  `/精华查询`、`/精华最近` 和 `/精华状态`。
- 插件默认只读，只允许配置中的管理员与群白名单，所有匹配指令均阻止进入 LLM。
- 插件通过当前 AIOCQHTTP 事件调用 OneBot Action，不保存 NapCat 地址或 Token。
- 支持 NapCat / go-cqhttp 兼容的 `get_essence_msg_list` 接口。
- 精华列表缺少发送时间或正文时，通过 `get_msg` 补全单条消息。
- OneBot 失败或没有数据时，可扫描截图目录并使用 Tesseract OCR。
- 可在不连接 NapCat、不创建数据库的情况下预览截图 OCR 质量。
- 保存发送者、发送时间、精华时间、设置人、正文、图片地址和原始响应。
- 按时间、昵称、QQ 号或正文进行本地搜索。
- 提供健康检查、触发采集和远程搜索 API。
- 使用稳定消息标识更新已有记录，重复采集不会无条件追加副本。
- 提供不联网的环境诊断、不写库采集预检和只读数据库审计。
- 使用显式数据库版本迁移，并可预览或修复旧记录中的可恢复字段。
- 支持来源、群号、内容类型、时间范围筛选以及 JSON/CSV 导出。
- 可将 OneBot 图片按内容哈希下载到本地、执行 OCR，并纳入正文搜索。
- OCR 默认识别原图，仅在结果为空或低置信度时尝试一次轻量灰度放大兜底。

## 运行要求

- AstrBot 插件：AstrBot 4.x、已经可用的 NapCat/AIOCQHTTP 连接和目标群。
- 独立应用：Python 3.10 或更高版本。
- 独立 OneBot HTTP 采集：可访问的 NapCat 或 go-cqhttp 服务及目标群号。
- 使用 OCR 时：Tesseract OCR 和需要的语言包。中文默认使用 `chi_sim+eng`。

独立应用中的 OneBot 与 OCR 可以只配置其中一种，默认优先 OneBot、失败后尝试
OCR。AstrBot 插件第一版不加载 OCR、图片下载或独立 HTTP 依赖。

## 安装

Windows PowerShell：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e .
```

Linux / macOS：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
```

独立 CLI/API/OCR 的运行时依赖由 `pyproject.toml` 管理。根目录
`requirements.txt` 专供 AstrBot 插件安装流程，第一版插件路径没有额外 PyPI
依赖。安装完成后验证命令入口：

```powershell
essence --help
```

也可以始终使用模块形式：

```powershell
python -m group_essence_extractor.cli --help
```

## AstrBot 插件部署

在 AstrBot WebUI 的插件管理页使用本仓库地址安装，或上传根目录同时包含
`main.py`、`metadata.yaml`、`_conf_schema.json`、`requirements.txt` 和 `src/` 的
插件压缩包。插件配置来自 AstrBot，不读取 `.env`。

首次安装必须保持：

```text
validation_mode = true
admin_ids = [管理员 QQ 号]
allowed_group_ids = [一个测试群号]
default_group_id = 测试群号（仅私聊需要）
enable_image_enrichment = false
enable_scheduled_sync = false
```

先由管理员执行：

```text
/精华状态
/精华验收
```

验收回复只有字段类型和聚合计数，不创建数据库。确认真实 OneBot 契约正常后，将
`validation_mode` 改为 `false`，再依次执行：

```text
/精华同步
/精华同步
/精华查询 脱敏关键词
/精华最近 5
```

第二次同步应主要为“未变化”。插件数据库按需位于 AstrBot 数据根目录下的
`plugin_data/astrbot_plugin_group_essence/group_essence.db`，不会读写本仓库默认的
`data/group_essence.db`。完整安装、验收、故障定位与回滚步骤见
[`docs/ASTRBOT_DEPLOYMENT.md`](docs/ASTRBOT_DEPLOYMENT.md)。

### 安装 Tesseract

Tesseract 不是 Python 包，需单独安装。安装中文识别时还需确保 `chi_sim` 语言包
可用。如果 `tesseract` 已在 `PATH` 中，`TESSERACT_CMD` 保持为空；否则将它设置
为可执行文件路径或安装目录，例如：

```dotenv
TESSERACT_CMD=C:/Program Files/Tesseract-OCR/tesseract.exe
```

## 配置

复制示例配置后再填写本机参数：

```powershell
Copy-Item example.env .env
```

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `DB_PATH` | `./data/group_essence.db` | SQLite 数据库路径 |
| `ONEBOT_BASE_URL` | `http://127.0.0.1:3000` | OneBot HTTP 地址 |
| `ONEBOT_ACCESS_TOKEN` | 空 | OneBot Bearer Token |
| `GROUP_ID` | 空 | OneBot 采集所需的目标群号 |
| `PREFER_ONEBOT` | `true` | 是否先尝试 OneBot |
| `FALLBACK_OCR` | `true` | OneBot 无数据或失败时是否 OCR |
| `OCR_LANG` | `chi_sim+eng` | Tesseract 语言组合 |
| `TESSERACT_CMD` | 空 | Tesseract 可执行文件或安装目录 |
| `SCREENSHOT_DIR` | `./data/screenshots` | OCR 截图目录 |
| `IMAGE_DIR` | `./data/images` | OneBot 图片附件的内容哈希缓存目录 |

相对路径以启动命令时的当前目录为基准。`.env` 已被 Git 忽略，不能提交访问令牌
等敏感信息。

## 使用

### 检查运行条件

```powershell
essence doctor
```

该命令检查 Python、数据库目录、OneBot 必填参数、Tesseract 和截图目录，但不会
连接 OneBot，也不会创建数据库或其他文件。`status=error` 时命令返回非零退出码。

准备执行图片补全时使用：

```powershell
essence doctor --images
```

该模式还会检查 Tesseract 和 `IMAGE_DIR` 的可写条件，仍然不会联网或创建目录。

### 离线预览截图 OCR

NapCat 暂不可用时，可以独立验证截图识别与字段解析：

```powershell
essence ocr-preview
essence ocr-preview --screenshot-dir ./data/screenshots --group-id "123456" --limit 10
```

该命令扫描 PNG、JPEG 和 WebP，最多处理 `--limit` 张截图；默认目录来自
`SCREENSHOT_DIR`。它会先识别校正方向后的原图，只有原图无文字或平均置信度低于
阈值时，才额外尝试一次三倍灰度放大，并选择质量更好的结果。解析器支持带
“发送者/发送时间”等标签的文本，也支持 QQ 精华卡片常见的“昵称、元数据、正文”
三段布局。

输出只包含候选数、处理数、错误数、字段缺失、平均置信度及识别/解析策略分布，
不会包含截图文件名、昵称、正文，也不会初始化或写入数据库。`status=warning`
表示至少一张截图失败，其余成功结果仍会进入质量统计。

### 初始化数据库

```powershell
essence init-db
```

初始化会按 `PRAGMA user_version` 依次执行幂等迁移。输出中的 `from_version`、
`to_version` 和 `applied` 可用于确认本次实际执行了哪些迁移；程序不会打开高于当前
支持版本的数据库。

### 执行一次采集

首次连接远端 OneBot 时，建议先进行不写库预检：

```powershell
essence ingest --dry-run
```

`--dry-run` 会执行真实来源读取和标准化，但不初始化或写入数据库，也不会在输出中
包含消息正文；它只报告来源数量、字段缺失、详情补全错误和待 OCR 图片数量。

确认质量统计后再正式写入：

```powershell
essence ingest
```

输出会区分数据来源与写入结果：

```json
{
  "dry_run": false,
  "collected": 12,
  "from_onebot": 12,
  "onebot_error": "",
  "from_ocr": 0,
  "ocr_error_count": 0,
  "quality": {
    "total": 12,
    "by_source": {"onebot": 12},
    "by_content_type": {"image": 2, "text": 10},
    "missing": {"group_id": 0, "sender_time": 0},
    "detail_errors": 0,
    "images_without_ocr": 2,
    "ocr_quality": {
      "records": 0,
      "structured_complete": 0,
      "mean_confidence": null,
      "by_parser_profile": {},
      "by_recognition_profile": {}
    }
  },
  "inserted": 2,
  "updated": 10,
  "unchanged": 0
}
```

OneBot 记录使用来源、群号和消息 ID 识别已有数据；OCR 记录使用截图内容的
SHA-256 指纹，并兼容按原截图路径更新旧记录。

### 审计现有数据库

```powershell
essence audit-db
```

审计使用 SQLite 只读连接，不创建或修改数据库。输出包含快速完整性检查、记录总数、
数据库版本、来源与内容类型分布、空字段、重复身份、发送/精华时间范围以及附件处理
状态。schema v2 使用独立附件表，不改变原始 `image_path`。

### 预览和修复旧数据

默认命令只读扫描，不修改数据库：

```powershell
essence repair-db
```

它会尝试从已保存的 `raw_json` 恢复缺失的群号、消息 ID、发送时间和精华时间，并
重建正文搜索字段。输出只包含候选数、无法恢复数和待更新行数，不包含消息正文。
确认预览后才显式写入：

```powershell
essence repair-db --apply
```

缺失群号的 OneBot 旧记录会优先使用原始响应；原始响应也没有群号时，使用
`--group-id` 或配置中的 `GROUP_ID`。无法确定的字段会保留原状并计入 `unresolved`。

### 补全图片内容

先进行只读、离线预览：

```powershell
essence enrich-images
essence enrich-images --group-id "123456" --limit 10
```

预览只解析数据库中已有的 OneBot 图片地址，报告待处理数、已完成数和不支持的地址；
不会请求图片、运行 Tesseract、迁移数据库或创建缓存目录。确认 Tesseract 与统计后再
显式执行：

```powershell
essence enrich-images --apply --limit 10
```

默认单张图片最大 20 MiB、请求超时 20 秒，可通过 `--max-bytes` 和 `--timeout`
调整。图片保存在 `IMAGE_DIR/<哈希前缀>/<SHA-256>.<扩展名>`，不同消息引用相同内容
时只保留一个文件。每个附件的远端地址、本地相对路径、哈希、大小、OCR 文本和状态
记录在附件表中；成功 OCR 会合并到消息的 `ocr_text` 与 `content_search`。

`completed` 和 `no_text` 不会重复处理；`failed` 会在下次执行时重试。若图片已经
下载但 OCR 暂时失败，重试会直接使用缓存。命令输出只有聚合统计，不包含图片地址或
OCR 正文。

### 本地搜索

```powershell
essence search --sender-time "2026-05-01"
essence search --essence-time "2026-05-01"
essence search --sender "张三"
essence search --sender-qq "10001"
essence search --operator "管理员"
essence search --operator-qq "10002"
essence search --content "活动通知"
essence search --group-id "123456" --source onebot --content-type mixed
essence search --sender-time-from "2026-05-01 00:00:00" --sender-time-to "2026-05-31 23:59:59"
essence search --essence-time-from "2026-05-01" --essence-time-to "2026-06-01"
essence search --content "活动" --limit 20 --offset 0
```

多个搜索条件同时出现时按 AND 组合。昵称、正文和单个时间文本使用包含匹配；QQ
号、群号、来源和内容类型使用精确匹配；`*-from` / `*-to` 使用闭区间。结果默认按
精华时间倒序排列，单次最多返回 1000 条。CLI 输出包含 `total`、当前页 `count`、
`limit`、`offset` 和 `items`。

### 导出搜索结果

```powershell
essence export --format json --output ./data/exports/essence.json
essence export --format csv --output ./data/exports/essence.csv --group-id "123456"
essence export --format json --output ./data/exports/recent.json `
  --essence-time-from "2026-05-01" --max-records 500
```

导出复用全部搜索筛选条件。JSON 使用 UTF-8，CSV 使用带 BOM 的 UTF-8 以方便表格
软件识别中文。已有文件默认不会被覆盖；确认后添加 `--force`。导出路径不能指向
当前 SQLite 数据库。

### HTTP API

默认只监听本机：

```powershell
essence serve --host 127.0.0.1 --port 8000
```

接口：

- `GET /health`：健康检查。
- `POST /api/v1/ingest`：触发一次采集。
- `POST /api/v1/search`：搜索已入库记录。

搜索示例：

```http
POST /api/v1/search
Content-Type: application/json

{
  "request_id": "req-001",
  "query": {
    "sender_time": "2026-05-01",
    "sender": "张三",
    "content": "活动",
    "group_id": "123456",
    "source": "onebot",
    "essence_time_from": "2026-05-01 00:00:00",
    "limit": 50,
    "offset": 0
  }
}
```

响应中的 `total` 是筛选后的总记录数，`count` 是当前页条数，便于客户端可靠分页。

当前 API 没有应用层鉴权。如需监听 `0.0.0.0`，应先在可信网络、反向代理或其他
访问控制之后部署，不建议直接暴露到公网。

## 项目与资源目录

```text
main.py                       AstrBot 插件命令入口
metadata.yaml                 AstrBot 插件元数据
_conf_schema.json             AstrBot WebUI 配置结构
src/group_essence_extractor/  Python 包与运行逻辑
tests/                        自动测试；公开且脱敏的夹具放 tests/fixtures/
docs/                         架构与开发补充文档；配图放 docs/assets/
data/                         本地数据库、截图、哈希图片缓存和导出文件（不提交）
.github/                      CI 与 Issue 模板
example.env                   可提交的配置模板
.env                          本地配置和密钥（不提交）
```

运行数据的详细规则见 `data/README.md`，内部模块和数据流见
`docs/ARCHITECTURE.md`。AstrBot 远端部署见 `docs/ASTRBOT_DEPLOYMENT.md`；仅在维护
独立 OneBot HTTP 客户端时才使用 `docs/REMOTE_VALIDATION.md`。

## 开发与测试

项目测试只使用 Python 标准库，无需额外测试依赖：

```powershell
python -m unittest discover -s tests -v
python -m pip check
```

其中 OneBot 契约测试会在本机随机端口启动临时 HTTP 服务，读取
`tests/fixtures/onebot/` 下的脱敏响应；不会访问真实 OneBot 或互联网。

贡献流程、测试夹具和隐私要求见 `CONTRIBUTING.md`。GitHub Actions 会在受支持的
最低 Python 3.10 和当前开发环境 Python 3.14 上执行相同测试。

## 已知限制

- AstrBot 插件第一版只允许配置的管理员主动触发，不启用定时同步、图片补全或截图
  OCR；这些能力仍由独立 CLI 提供。
- 截图 OCR 尚未进行界面区域分割；未覆盖的卡片布局、严重裁切或低清图片仍可能需要
  新增解析策略。
- 已失效或需要额外登录态的 OneBot 图片地址会记录为失败，需在可访问图片的环境重试。
- 正文检索使用 SQLite `LIKE`，数据量较大时可升级为 FTS5。
- OneBot 不同实现的返回字段可能存在差异；补全逻辑目前面向标准
  `get_essence_msg_list` 与 `get_msg` 响应。
- API 尚未内置用户系统、签名或访问令牌校验。

## 许可证

本项目使用 MIT License，详见 `LICENSE`。
