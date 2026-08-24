# 贡献指南

## 开发环境

项目支持 Python 3.10 及以上版本。创建独立虚拟环境并安装项目：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

运行时依赖只在 `pyproject.toml` 中维护，`requirements.txt` 负责安装当前项目，
不要在两处重复添加版本清单。

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

HTTP 契约测试可以在 `127.0.0.1` 随机端口启动临时服务，但必须在测试结束时关闭，
且响应数据只能来自 `tests/fixtures/` 下的脱敏固定夹具。

少量、公开且完成脱敏的固定输入可以放入 `tests/fixtures/`。真实 QQ 截图、群号、
昵称、消息正文、访问令牌和原始数据库不得作为测试夹具提交。

## 文件与文档

- 本地运行数据放在 `data/`，该目录除规则说明外不会进入 Git。
- 文档配图放在 `docs/assets/`。
- 配置模板写入 `example.env`，本机值写入 `.env`。
- CI、Issue 和 Pull Request 配置放在 `.github/`，必须纳入版本控制。

涉及模块边界、数据身份或存储流程的改动，应同步更新
`docs/ARCHITECTURE.md`；面向使用者的行为变化应同步更新 `README.md`。

## 问题报告

提交 Issue 时请提供可复现步骤、预期结果、实际结果和相关版本。日志、请求响应
和截图必须先脱敏；不要发布令牌或真实群聊隐私数据。
