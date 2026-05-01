# Group Essence Extractor

一个简单的 QQ 群精华消息提取器，满足以下目标：

- 优先通过 OneBot HTTP 接口（NapCat / go-cqhttp）拉取精华消息
- 当接口不可用或无数据时，自动回退到截图 OCR
- 将发送者、发送时间、精华时间、设置人、消息内容、图片/OCR 文本统一入库
- 支持本地搜索（CLI）和远程搜索（HTTP API）

## 1. 环境准备（venv）

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

如果你希望通过 OCR 处理中文，请额外安装 Tesseract，并配置中文语言包 `chi_sim`。
可能需要更改python设置，以便在虚拟环境中使用环境变量中的Tesseract。

## 2. 配置

复制配置：

```powershell
copy example.env .env
```

关键配置项：

- `PREFER_ONEBOT=true`: 优先从 OneBot 拉取
- `FALLBACK_OCR=true`: OneBot 失败时回退 OCR
- `SCREENSHOT_DIR=./data/screenshots`: 放截图目录
- `DB_PATH=./data/group_essence.db`: SQLite 库文件

把你的示例图（如 `example.png`）放到 `data/screenshots`。

## 3. 使用

### 3.1 初始化数据库

```powershell
python -m group_essence_extractor.cli init-db
```

### 3.2 采集并入库（OneBot 或 OCR）

```powershell
python -m group_essence_extractor.cli ingest
```

### 3.3 本地搜索

按发送时间/精华时间搜索：

```powershell
python -m group_essence_extractor.cli search --sender-time "2026-05-01"
python -m group_essence_extractor.cli search --essence-time "2026-05-01"
```

按发送人/设置人搜索：

```powershell
python -m group_essence_extractor.cli search --sender "咕咕嘎嘎"
python -m group_essence_extractor.cli search --operator "管理员"
```

按 QQ 号搜索（精确匹配）：

```powershell
python -m group_essence_extractor.cli search --sender-qq "114514"
python -m group_essence_extractor.cli search --operator-qq "1919810"
```

按内容搜索（图片精华走 OCR 文本匹配）：

```powershell
python -m group_essence_extractor.cli search --content "不赖"
```

### 3.4 远程搜索接口

启动 API：

```powershell
python -m group_essence_extractor.cli serve --host 0.0.0.0 --port 8000
```

健康检查：

```http
GET /health
```

搜索接口（固定格式，可继续迭代）：

```http
POST /api/v1/search
Content-Type: application/json

{
  "request_id": "req-001",
  "query": {
    "sender_time": "2026-04-23",
    "essence_time": "",
    "sender": "",
    "sender_qq": "",
    "operator": "",
    "operator_qq": "",
    "content": "活动",
    "limit": 50,
    "offset": 0
  }
}
```

响应：

```json
{
  "request_id": "req-001",
  "status": "ok",
  "count": 1,
  "items": [
    {
      "id": 1,
      "sender": "张三",
      "sender_time": "2026-04-23 10:20:00",
      "essence_time": "2026-04-23 10:30:00",
      "operator": "管理员A",
      "content_text": "活动通知...",
      "content_type": "text",
      "ocr_text": "",
      "image_path": "",
      "source": "onebot"
    }
  ]
}
```

## 4. 当前实现说明

- OneBot 适配采用 `POST /get_essence_msg_list`。
- OCR 回退会扫描 `data/screenshots` 下的图片，先做全文 OCR，再用规则抽取字段。
- 图片型精华消息会将图片路径和 OCR 文本共同入库，检索时匹配 `content_search` 字段。
- 推荐采用onebot入库方式，目前尚未测试大量使用OCR入库是否能正常运行。
- 图片类精华消息会以URL形式给出。

## 5. 后续可增强

- 基于 UI 区域分割提升截图字段抽取准确率
- 对 OneBot 图片 URL 自动下载并单图 OCR
- 内容检索升级为 FTS5 全文索引
- 增加请求签名/鉴权，安全接入外部 API 服务器
- 验证仅OCR进行大范围识别入库的可行性与稳定性
