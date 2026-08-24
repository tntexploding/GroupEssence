# Group Essence Extractor

一个面向 QQ 群精华消息的本地采集与检索工具。程序优先通过 OneBot HTTP
接口获取精华消息，在接口不可用或没有返回数据时可回退到截图 OCR，最终将统一
后的记录写入 SQLite，并提供命令行和 HTTP API 两种检索方式。

## 功能

- 支持 NapCat / go-cqhttp 兼容的 `get_essence_msg_list` 接口。
- 精华列表缺少发送时间或正文时，通过 `get_msg` 补全单条消息。
- OneBot 失败或没有数据时，可扫描截图目录并使用 Tesseract OCR。
- 保存发送者、发送时间、精华时间、设置人、正文、图片地址和原始响应。
- 按时间、昵称、QQ 号或正文进行本地搜索。
- 提供健康检查、触发采集和远程搜索 API。
- 使用稳定消息标识更新已有记录，重复采集不会无条件追加副本。

## 运行要求

- Python 3.10 或更高版本。
- 使用 OneBot 时：可访问的 NapCat 或 go-cqhttp 兼容 HTTP 服务，以及目标群号。
- 使用 OCR 时：Tesseract OCR 和需要的语言包。中文默认使用 `chi_sim+eng`。

OneBot 与 OCR 可以只配置其中一种。默认行为是优先 OneBot，失败后尝试 OCR。

## 安装

Windows PowerShell：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

Linux / macOS：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

`requirements.txt` 会以 editable 模式安装当前项目，运行时依赖统一由
`pyproject.toml` 管理。安装完成后验证命令入口：

```powershell
essence --help
```

也可以始终使用模块形式：

```powershell
python -m group_essence_extractor.cli --help
```

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

相对路径以启动命令时的当前目录为基准。`.env` 已被 Git 忽略，不能提交访问令牌
等敏感信息。

## 使用

### 初始化数据库

```powershell
essence init-db
```

### 执行一次采集

```powershell
essence ingest
```

输出会区分数据来源与写入结果：

```json
{
  "from_onebot": 12,
  "onebot_error": "",
  "from_ocr": 0,
  "ocr_error_count": 0,
  "inserted": 2,
  "updated": 10,
  "unchanged": 0
}
```

OneBot 记录使用来源、群号和消息 ID 识别已有数据；OCR 记录使用截图内容的
SHA-256 指纹，并兼容按原截图路径更新旧记录。

### 本地搜索

```powershell
essence search --sender-time "2026-05-01"
essence search --essence-time "2026-05-01"
essence search --sender "张三"
essence search --sender-qq "10001"
essence search --operator "管理员"
essence search --operator-qq "10002"
essence search --content "活动通知"
essence search --content "活动" --limit 20 --offset 0
```

多个搜索条件同时出现时按 AND 组合。昵称和正文使用包含匹配，QQ 号使用精确
匹配；结果默认按精华时间倒序排列，单次最多返回 1000 条。

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
    "limit": 50,
    "offset": 0
  }
}
```

当前 API 没有应用层鉴权。如需监听 `0.0.0.0`，应先在可信网络、反向代理或其他
访问控制之后部署，不建议直接暴露到公网。

## 项目与资源目录

```text
src/group_essence_extractor/  Python 包与运行逻辑
tests/                        自动测试；公开且脱敏的夹具放 tests/fixtures/
docs/                         架构与开发补充文档；配图放 docs/assets/
data/                         本地数据库、待识别截图和下载图片（不提交）
.github/                      CI 与 Issue 模板
example.env                   可提交的配置模板
.env                          本地配置和密钥（不提交）
```

运行数据的详细规则见 `data/README.md`，内部模块和数据流见
`docs/ARCHITECTURE.md`。

## 开发与测试

项目测试只使用 Python 标准库，无需额外测试依赖：

```powershell
python -m unittest discover -s tests -v
python -m pip check
```

贡献流程、测试夹具和隐私要求见 `CONTRIBUTING.md`。GitHub Actions 会在受支持的
最低 Python 3.10 和当前开发环境 Python 3.14 上执行相同测试。

## 已知限制

- 截图 OCR 仍是整图识别加规则提取，复杂界面或低清图片的字段准确率有限。
- OneBot 图片地址会保存，但尚未自动下载并逐图 OCR。
- 正文检索使用 SQLite `LIKE`，数据量较大时可升级为 FTS5。
- OneBot 不同实现的返回字段可能存在差异；补全逻辑目前面向标准
  `get_essence_msg_list` 与 `get_msg` 响应。
- API 尚未内置用户系统、签名或访问令牌校验。

## 许可证

本项目使用 MIT License，详见 `LICENSE`。
