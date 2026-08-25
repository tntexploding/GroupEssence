# 独立 OneBot HTTP 客户端验收指南

本文只适用于维护独立 CLI 的 OneBot HTTP 直连能力。当前生产式远端验证应优先作为
AstrBot 插件部署，复用 AstrBot 已有的 NapCat 连接；请改用
[`ASTRBOT_DEPLOYMENT.md`](./ASTRBOT_DEPLOYMENT.md)。不要为了插件验收额外开放
NapCat HTTP 端口或配置第二份 Token。

以下流程用于确实需要独立客户端时，在能够登录 QQ 的受控环境验证真实 OneBot
响应。目标是先检查、再预览、最后写入，避免在远端临时修改代码或污染数据库。

## 1. 安装与配置

```powershell
git pull --ff-only
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e .
Copy-Item example.env .env
```

至少填写：

```dotenv
ONEBOT_BASE_URL=http://127.0.0.1:3000
ONEBOT_ACCESS_TOKEN=
GROUP_ID=目标群号
PREFER_ONEBOT=true
IMAGE_DIR=./data/images
```

不要将远端 `.env`、数据库或真实响应提交到 Git。

## 2. 本地条件检查

```powershell
essence doctor
```

该命令不会联网。确认 Python、数据库路径和 OneBot 配置为 `ok`。如果远端不准备
使用 OCR，可以设置 `FALLBACK_OCR=false`，避免 Tesseract 缺失影响诊断结果。
准备验证 OneBot 图片 OCR 时，还需安装对应语言包并执行：

```powershell
essence doctor --images
```

### NapCat 不可用时先验证截图 OCR

截图 OCR 与 OneBot 连接相互独立，可在本地或远端先执行：

```powershell
essence ocr-preview --screenshot-dir ./data/screenshots --group-id "目标群号" --limit 10
```

该命令不会创建或写入数据库，也不会输出截图名、昵称或正文。重点检查
`ocr_error_count`、`quality.missing`、`quality.ocr_quality.structured_complete`、
`mean_confidence` 和两类策略分布。QQ 精华卡片通常应进入
`qq_essence_card` 解析策略；只有低置信度图片才应进入 `scale3_gray` 识别策略。
截图属于 `data/` 下的本地运行数据，验收后无需移动到测试夹具或提交到 Git。

## 3. 不写库采集预检

```powershell
essence ingest --dry-run
```

重点检查：

- `onebot_error` 应为空；
- `from_onebot` 和 `collected` 应大于 0；
- `quality.missing.group_id`、`sender_time`、`message_id` 应尽量为 0；
- `quality.detail_errors` 应为 0；
- 输出不得包含真实消息正文。

若失败，只保留错误信息和脱敏后的字段名称/类型；不要复制令牌、昵称或正文到 Issue。

## 4. 写入前审计

已有数据库时先执行：

```powershell
essence audit-db
essence repair-db
```

记录 schema 版本、总数、空字段和重复身份数量。`repair-db` 此时只读预览；重点核对
`would_update`、各字段 `candidates` 和 `unresolved`。如确实要补全旧数据，确认数据库
已有一次可恢复备份后再执行：

```powershell
essence repair-db --apply
```

只有原始响应和 `.env` 都没有群号时，才需要显式传入
`--group-id "目标群号"`。不要反复生成测试存档，也不要将数据库或预览之外的原始
数据复制回源码仓库。

## 5. 正式采集与幂等验证

```powershell
essence ingest
essence ingest
```

第一次可能产生 `inserted` 和 `updated`；没有新精华消息时，第二次应主要表现为
`unchanged`，不应再次新增相同消息。

完成后再次运行：

```powershell
essence audit-db
essence search --content "用于验收的脱敏关键字" --limit 10
```

核对 `total/count`、发送时间、精华时间、群号、用户 ID、正文类型和图片地址。若需
验证范围查询，可追加 `--group-id`、`--source onebot` 和 `--essence-time-from`，无需
为此创建额外数据库或导出文件。

## 6. 图片补全小批量验收

图片地址可能带短期签名，建议在正式采集后尽快预览：

```powershell
essence enrich-images --group-id "目标群号" --limit 10
```

预览不会请求图片或写库。确认 `discovered`、`pending` 和 `selected` 符合预期后，
只处理一个小批次：

```powershell
essence enrich-images --apply --group-id "目标群号" --limit 10
essence audit-db
essence enrich-images --group-id "目标群号" --limit 10
```

第一次执行应产生 `downloaded` 以及 `ocr_completed`、`no_text` 或 `failed`；第二次
预览中成功项应进入 `already_complete`。`audit-db.attachments` 应显示相同的状态分布。
不要为了测试反复删除缓存；失败项再次执行会复用已成功下载的文件。

若图片返回 403/404，记录状态即可，不要将真实 URL 带回本地。需要额外 Cookie 或
登录态的图片源暂不在本轮兼容范围内。

## 7. 验收反馈

远端发现兼容差异时，建议只带回以下信息：

- NapCat 版本；
- action 名称；
- HTTP 状态码和 OneBot `status/retcode`；
- 脱敏后的 JSON 字段名及值类型；
- `dry-run` 质量统计；
- 图片补全的聚合状态计数。

不要带回访问令牌、Cookie、真实群号、QQ 号、昵称、消息正文或图片 URL。
