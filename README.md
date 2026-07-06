# QQbot_v2

基于 NapCat + OneBot v11 + FastAPI 的 QQ 机器人。

当前目标是：登录一个 QQ 账号，把 NapCat 作为消息入口，接入 DeepSeek API，实现群聊和私聊的自动问答、会话隔离、后台配置、长期记忆、命令管理、日志、备份和统计。

## 功能

- NapCat 反向 WebSocket 接入 QQ 消息。
- 群聊和私聊分会话隔离，私聊也作为独立会话。
- DeepSeek OpenAI-compatible API 调用。
- 网页后台配置 API、人设、会话、记忆、命令库、备份和日志。
- 支持全局人设和单独会话人设。
- 支持管理员长期记忆和会话学习记忆。
- 支持群聊触发模式：仅 @、前缀、全部消息。
- 支持会话启用 / 停用、回复冷却、回复概率、每小时回复上限。
- 支持本地日志、备份、恢复、统计表格和折线图。
- 支持管理员命令和群聊真正 @ 指定 QQ。

## 环境

- Windows 10/11
- PowerShell
- Python 3.11 或更高版本
- NapCat

Python 依赖在首次启动时会自动安装到项目内 `.venv`。

## 启动

进入项目目录：

```powershell
cd 你的仓库目录
```

启动：

```powershell
.\start.ps1
```

也可以双击仓库里的快捷启动脚本：

```text
start-qqbot-v2.bat
```

启动成功后会自动打开后台网页，并在命令行输出：

```text
Local web: http://127.0.0.1:6185/
NapCat reverse websocket: ws://127.0.0.1:6199/onebot/ws
```

后台地址：

```text
http://127.0.0.1:6185/
```

如果不想自动打开浏览器，可以运行：

```powershell
.\start.ps1 -NoOpenBrowser
```

## NapCat 配置

在 NapCat 中添加反向 WebSocket，地址填：

```text
ws://127.0.0.1:6199/onebot/ws
```

如果机器人和 NapCat 不在同一台机器，把 `127.0.0.1` 改成运行 QQbot_v2 的机器 IP。

## 后台配置

打开后台：

```text
http://127.0.0.1:6185/
```

至少需要配置：

- `Base URL`：默认 `https://api.deepseek.com`
- `API Key`：你的 DeepSeek API Key
- `Model`：`deepseek-v4-flash` 或 `deepseek-v4-pro`
- `管理员 QQ`：允许执行管理命令的 QQ 号，多个用英文逗号分隔

保存后，API Key 会写入本地 `config.local.json`，后台不会明文回显。

## 测试

群聊收发测试：

```text
@机器人 测试
```

如果返回下面内容，说明 OneBot 通道正常：

```text
QQbot_v2 已收到群消息，OneBot 通道正常。
```

管理员群聊 @ 测试：

```text
@机器人 /at 目标QQ号 测试一下
```

例如：

```text
@机器人 /at 123456789 测试一下
```

私聊测试：

```text
测试
```

命令帮助：

```text
/帮助
```

## 常用命令

- `/帮助`：查看当前可用命令。
- `/状态`：查看当前会话状态，管理员专用。
- `/启用`：启用当前会话机器人，管理员专用。
- `/停用`：停用当前会话机器人，管理员专用。
- `/模式 查看`：查看当前群聊回复模式，管理员专用。
- `/模式 @机器人`：群聊仅 @ 机器人时回复，管理员专用。
- `/模式 前缀 /bot`：群聊使用指定前缀触发，管理员专用。
- `/模式 全部消息`：所有消息都触发回复，管理员专用。
- `/人设 查看`：查看当前会话人设，管理员专用。
- `/人设 设置 内容`：设置当前会话人设，管理员专用。
- `/记忆 查看`：查看当前会话管理员长期记忆，管理员专用。
- `/记忆 添加 内容`：添加当前会话管理员长期记忆，管理员专用。
- `/学习 查看`：查看当前会话学习记忆，管理员专用。
- `/学习 更新`：立即更新当前会话学习记忆，管理员专用。
- `/备份`：创建本地备份，管理员专用。
- `/备份列表`：查看最近备份，管理员专用。
- `/健康检查`：查看运行健康状态，管理员专用。
- `/at QQ号 内容`：群聊真正 @ 指定 QQ，管理员专用。

## 本地文件

真实本地配置：

```text
config.local.json
```

示例配置：

```text
config.example.json
```

命令库：

```text
data/commands.json
```

本地数据：

```text
data/bot.sqlite3
data/memories/
logs/
backups/
```

## GitHub 上传前检查

本项目只需要上传 `QQbot_v2` 目录。

上传前运行：

```powershell
.\scripts\preflight-upload.ps1
```

不要上传这些本地文件：

```text
config.local.json
.venv/
data/bot.sqlite3
data/memories/
logs/
backups/
*.lnk
```

这些已经写入 `.gitignore`。如果上传前检查失败，先处理提示的问题再提交。

## 部署到新机器

1. 安装 Python 3.11 或更高版本。
2. 安装并登录 NapCat。
3. 克隆仓库，进入仓库目录。
4. 运行 `.\start.ps1`，或双击 `start-qqbot-v2.bat`。
5. 打开 `http://127.0.0.1:6185/`。
6. 填入 DeepSeek API Key、模型、管理员 QQ。
7. 在 NapCat 中配置反向 WebSocket：`ws://127.0.0.1:6199/onebot/ws`。
8. 在 QQ 群或私聊发送测试消息。

## 注意

- `config.local.json` 只保存本机真实配置，不提交。
- 聊天记录、记忆、日志、备份都只保存在本地，不提交。
- 群聊和私聊记忆隔离，不同群之间不会串记忆。
- 命令逻辑在 `data/commands.json` 和 `app/command_handlers.py`，不要塞回全局人设。
