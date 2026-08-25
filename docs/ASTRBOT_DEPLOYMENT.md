# AstrBot 远端部署与验收

本项目可直接作为 AstrBot 插件安装。插件复用 AstrBot 已经建立的
NapCat/AIOCQHTTP（OneBot v11）连接，通过当前消息事件调用 OneBot Action；不启动
FastAPI/Uvicorn，不新增监听端口，也不读取 `ONEBOT_BASE_URL` 或
`ONEBOT_ACCESS_TOKEN`。

## 1. 部署前检查

1. 确认远端 AstrBot 已能通过 NapCat 正常收发测试群消息。
2. 备份 AstrBot 数据卷，尤其是已有的 `plugin_data/`。
3. 确认测试管理员 QQ 号和一个测试群号，暂时不要加入更多群。
4. 保持 NapCat 和 AstrBot 的现有连接配置不变。

插件运行路径只使用 Python 标准库与 AstrBot 提供的 API，根目录
`requirements.txt` 没有第三方依赖。独立 CLI/API/OCR 所需依赖不应安装进 AstrBot
容器来完成本轮验收。

## 2. 安装插件

优先在 AstrBot WebUI 的插件管理页使用仓库地址安装：

```text
https://github.com/tntexploding/GroupEssence
```

如果当前 AstrBot 版本只支持上传插件压缩包，压缩包根目录必须直接包含：

```text
main.py
metadata.yaml
_conf_schema.json
requirements.txt
src/
```

不要只上传 `src/group_essence_extractor/`，也不要在插件目录中创建 `.env`。安装或更新
后，通过 WebUI 重新加载插件；若热重载未生效，再重启 AstrBot 容器一次。

## 3. 阶段 A：只读契约验收

先在插件配置中填写：

```json
{
  "validation_mode": true,
  "admin_ids": ["管理员 QQ 号"],
  "allowed_group_ids": ["测试群号"],
  "default_group_id": "测试群号",
  "max_validation_detail_requests": 10,
  "max_sync_detail_requests": 10,
  "history_query_limit": 100,
  "max_query_results": 5,
  "max_content_chars": 300,
  "enable_image_enrichment": false,
  "enable_scheduled_sync": false
}
```

`allowed_group_ids` 为空时会拒绝所有群；私聊命令只有在
`default_group_id` 同时位于白名单时才有目标群。第一版所有指令都只允许
`admin_ids` 中的账号。

使用测试管理员执行：

```text
/精华状态
/精华验收
```

私聊且未配置默认群时，可显式执行 `/精华验收 测试群号`。验收命令只调用
`get_essence_msg_list`，并且只在正文缺失时用 `get_msg` 补全。验收最多发出
`max_validation_detail_requests` 次详情请求（范围 0–50，默认 10）；它不创建数据库
或数据目录。报告中的详情统计分别表示候选、实际请求、因上限或缺少消息 ID 而跳过、
以及失败的数量。
回复应只有数量、内容类型、缺失字段、字段类型和详情补全计数，不应出现群号、QQ
号、昵称、正文、图片地址或原始响应。

阶段 A 通过条件：

- 精华数量与群内实际情况相符；
- `message_id`、发送时间、精华时间和正文缺失量已被准确报告；
- 单条 `get_msg` 失败不会使整批验收失败；
- 普通聊天仍按原 AstrBot 流程处理，精华指令不会进入 LLM；
- AstrBot 日志只有 action 状态、异常类别和聚合数量，没有隐私正文或凭据；
- AstrBot 与 NapCat 没有持续的 CPU 或内存增长。

NapCat 可能不在精华列表中提供历史 `sender_time`，且旧消息已经无法由 `get_msg`
回查。阶段 A 仍把发送时间保留为质量字段，不触发群历史查询，也不会用
`essence_time` 代填；真实时间补全在阶段 B 由有界群历史匹配完成。

## 4. 阶段 B：启用同步与查询

阶段 A 通过后，将 `validation_mode` 改为 `false` 并重新加载插件，然后执行：

```text
/精华同步
/精华同步
/精华补全时间 100
/精华查询 脱敏测试关键词
/精华最近 5
/精华状态
```

第一次同步可以出现 `新增` 或 `更新`；没有新精华时，紧接着的第二次同步应主要为
`未变化`，不能再次新增同一批记录。OneBot 原始结构或短期图片地址变化只计入
`元数据刷新`。同步的正文详情请求受 `max_sync_detail_requests` 限制，并跳过数据库中
已经记录失败的旧消息，避免每次同步重复请求同一批历史详情。

对新发现且缺少发送时间的精华，同步最多调用一次 `get_group_msg_history`，并只保留
消息 ID、序号、随机号和时间用于匹配；群历史正文和发送者不会进入时间索引或回复。
历史接口失败不会阻断同步，报告会显示“历史查询失败：是”。已有数据库中的缺失时间
使用 `/精华补全时间 [数量]` 显式处理，数量为 1 到 `history_query_limit`；只更新空值，
按消息 ID、序号与随机号或无歧义序号匹配，无法确定的记录保持为空。

查询始终限定当前授权群，空关键词会被拒绝，“最近”的数量只接受 1–20。

数据库按需创建在 AstrBot 数据根目录下：

```text
plugin_data/astrbot_plugin_group_essence/group_essence.db
```

它不位于插件源码目录，也不使用本仓库的 `data/group_essence.db`。完成一次同步后重启
AstrBot，再执行 `/精华最近 5`，确认数据卷中的记录仍可读取。

## 5. 故障定位

远端反馈只保留以下信息：

- AstrBot、NapCat 和插件版本；
- 失败的指令和 action 名称；
- OneBot `status`、`retcode` 及脱敏 wording；
- 采集数量、缺失计数、详情补全失败计数；
- 时间补全扫描、匹配、更新和剩余数量；
- 异常类别和数据库 schema 版本。

不要复制 Access Token、Cookie、完整 QQ 号或群号、昵称、消息正文、图片 URL、原始
OneBot JSON、数据库路径或数据库文件。插件没有单独的 HTTP 地址；若
`get_essence_msg_list` 不可用，应先检查 AstrBot 当前 AIOCQHTTP 适配器和 NapCat
版本，而不是为插件新增 HTTP 监听。

## 6. 回滚

1. 在 AstrBot WebUI 禁用插件，确认普通对话与其他插件恢复正常。
2. 保留 `plugin_data/astrbot_plugin_group_essence/`，不要通过删库解决兼容问题。
3. 回退插件版本；重新启用前确认旧版支持数据库当前的 `PRAGMA user_version`。
4. 如需采集现场信息，只复制脱敏后的聚合统计，不复制数据卷内容。

当前 schema 版本由核心迁移代码管理。更新插件不会自动删除数据；禁用插件也不会
删除持久化目录。

## 7. 本轮边界

远端先完成阶段 A、B。当前发送时间修复只检查单次有界群历史，不自动分页追溯全部
历史。截图 OCR、OneBot 图片下载、计划同步和普通成员查询仍保持关闭。独立 CLI/API
只用于离线维护；只有未来出现 AstrBot 之外的远程调用方时，才评估部署额外服务。
