# 远端 NapCat 验收指南

本流程用于代码在本地完成测试后，在能够登录 QQ 的远端环境验证真实 OneBot 响应。
目标是先检查、再预览、最后写入，避免在远端临时修改代码或盲目污染数据库。

## 1. 安装与配置

```powershell
git pull --ff-only
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item example.env .env
```

至少填写：

```dotenv
ONEBOT_BASE_URL=http://127.0.0.1:3000
ONEBOT_ACCESS_TOKEN=
GROUP_ID=目标群号
PREFER_ONEBOT=true
```

不要将远端 `.env`、数据库或真实响应提交到 Git。

## 2. 本地条件检查

```powershell
essence doctor
```

该命令不会联网。确认 Python、数据库路径和 OneBot 配置为 `ok`。如果远端不准备
使用 OCR，可以设置 `FALLBACK_OCR=false`，避免 Tesseract 缺失影响诊断结果。

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
```

记录总数、空字段和重复身份数量。如果数据库包含重要数据，可以在首次远端写入前做
一次人工备份，但不要反复生成测试存档。

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

核对发送时间、精华时间、群号、用户 ID、正文类型和图片地址。

## 6. 验收反馈

远端发现兼容差异时，建议只带回以下信息：

- NapCat 版本；
- action 名称；
- HTTP 状态码和 OneBot `status/retcode`；
- 脱敏后的 JSON 字段名及值类型；
- `dry-run` 质量统计。

不要带回访问令牌、Cookie、真实群号、QQ 号、昵称、消息正文或图片 URL。
