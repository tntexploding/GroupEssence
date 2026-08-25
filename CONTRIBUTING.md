# 贡献指南

## 开发环境

项目支持 Python 3.10 及以上版本。创建独立虚拟环境并安装项目：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e .
```

独立应用依赖只在 `pyproject.toml` 中维护；根目录 `requirements.txt` 仅声明 AstrBot
插件运行路径的额外依赖，不得把 FastAPI、Pydantic、Pillow 或 AstrBot 自身版本固定
到其中。

## 开发流程

1. 从最新的 `main` 创建功能分支，例如 `feature/onebot-parser`。
2. 将一次改动控制在清晰、可验证的范围内。
3. 修改行为时同步添加或更新 `tests/` 下的测试。
4. 更新受影响的 README、配置示例或架构文档。
5. 提交前执行完整测试并确认 `git status` 不包含运行数据或临时文件。

推荐使用简洁的 Conventional Commits 风格提交信息，例如：

```text
fix: 补全 OneBot 精华消息发送时间
docs: 更新 OCR 安装说明
```

## 代码约定

- 保持 Python 3.10 兼容，不依赖仅在更新版本中出现的语法。
- 新逻辑优先使用小函数和明确的数据边界，避免在 CLI 或 API 层复制业务逻辑。
- OneBot 字段先标准化为字符串 ID，再写入 `EssenceMessage`。
- 传输无关的 OneBot 字段处理放在 `normalization.py`；HTTP 客户端和 AstrBot Action
  适配器不得各自维护一套解析规则。
- AstrBot `main.py` 只做命令、授权、日志和回复编排，不直接运行同步 SQLite API。
- 每个匹配的插件指令必须在处理器入口立即调用 `event.stop_event()`。
- 外部响应和 OCR 可能不完整；正常缺失应有可理解的回退行为。
- 未经讨论不要引入体积较大的运行时依赖。

## 测试

完整测试命令：

```powershell
python -m unittest discover -s tests -v
python -m pip check
essence --help
```

测试必须使用临时目录、内存对象或 mock，不得连接真实 OneBot 服务、修改
`data/group_essence.db`，也不得把缓存和测试输出留在仓库中。

数据库结构变更必须新增递增的 `PRAGMA user_version` 迁移，并覆盖旧版本升级、重复
执行无副作用和数据保留测试。修复及导出测试只允许写入系统临时目录；修复预览应
额外验证数据库文件未被修改。

HTTP 契约测试可以在 `127.0.0.1` 随机端口启动临时服务，但必须在测试结束时关闭，
且响应数据只能来自 `tests/fixtures/` 下的脱敏固定夹具。

AstrBot Action 与插件入口测试使用 fake event、fake async `call_action` 和注入的最小
AstrBot 模块，不安装或连接真实 AstrBot/NapCat。测试必须覆盖管理员与群白名单、
只读模式、所有指令的事件拦截、同步幂等、查询群隔离、回复脱敏、SQLite 线程卸载和
并发同步串行化。还必须覆盖正文存在但发送时间缺失时不调用 `get_msg`，以及验收详情
请求上限。插件测试数据只写系统临时目录。

图片补全测试同样只能使用回环地址和系统临时目录，并通过 mock 返回 OCR 文本，不得
调用真实 Tesseract、QQ 图片 CDN 或现有 `data/images/`。测试结束后必须关闭 HTTP
服务并删除哈希缓存。

截图 OCR 引擎测试使用系统临时目录生成的合成图片，并模拟 Tesseract TSV 结果；
布局解析测试使用脱敏合成文本。需要人工核对真实截图时只运行 `ocr-preview`，确认
命令没有写库且只输出聚合统计，不得把截图或识别全文加入测试快照。

少量、公开且完成脱敏的固定输入可以放入 `tests/fixtures/`。真实 QQ 截图、群号、
昵称、消息正文、访问令牌和原始数据库不得作为测试夹具提交。

## 文件与文档

- 本地运行数据放在 `data/`，该目录除规则说明外不会进入 Git。
- OneBot 图片按内容哈希存放在 `data/images/`，数据库仅保存相对缓存路径。
- 用户导出的 JSON/CSV 放在 `data/exports/`，不作为测试快照提交。
- 文档配图放在 `docs/assets/`。
- 配置模板写入 `example.env`，本机值写入 `.env`。
- CI、Issue 和 Pull Request 配置放在 `.github/`，必须纳入版本控制。
- AstrBot 插件入口与元数据放在仓库根目录；插件持久化数据必须使用 AstrBot 数据根
  目录下的 `plugin_data/astrbot_plugin_group_essence/`，不得写入源码目录。
- `requirements.txt` 只列 AstrBot 插件额外依赖；独立 CLI/API/OCR 依赖由
  `pyproject.toml` 管理。

涉及模块边界、数据身份或存储流程的改动，应同步更新
`docs/ARCHITECTURE.md`；面向使用者的行为变化应同步更新 `README.md`。

## 问题报告

提交 Issue 时请提供可复现步骤、预期结果、实际结果和相关版本。日志、请求响应
和截图必须先脱敏；不要发布令牌或真实群聊隐私数据。
