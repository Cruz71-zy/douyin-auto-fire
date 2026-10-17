# 抖音每日续火花：GitHub Actions 安全版

一个账号、每天北京时间 20:00、每位明确配置的好友最多一条文字消息。无需购买服务器，电脑和手机不必长期在线。默认 Dry Run，不会自动启用真实发送。

基于 [unmev/douyin-auto-fire](https://github.com/unmev/douyin-auto-fire) 二次开发，保留原作者版权与 PolyForm Noncommercial 1.0.0 许可证；个人非商业用途使用，商用须另行核查授权。

**先阅读 [中文部署教程](docs/github-actions.md)。** 教程包含 Windows 11 本人扫码、两个 Secrets、首次状态初始化、手动测试、正式发送开关及常见问题。

## 本版本行为

- `.github/workflows/send.yml`：`0 20 * * *`，`timezone: Asia/Shanghai`，支持手动触发与同仓库串行执行。
- `run.py`：默认只检查登录、好友唯一性、聊天标题、个人主页标识和输入框；不触发发送。
- 兼容原项目 `friends` / `targets`、`messages[].type=text`、`value` / `content` 与 `random.choices` 数据结构。多个消息作为候选，每人每天只选择其中一条；最小版本不发送图片、群聊或原生贴纸。
- 正式发送必须在 `targets` 中填写本人核对的好友主页 URL `/user/` 后的 `sec_uid`。仅昵称配置兼容 Dry Run，也必须找到唯一精确匹配；通过可见个人主页链接或点击唯一可见头像打开的独立主页验证身份，搜索与聊天中的标识必须一致。不会回退到模糊匹配、群聊或隐藏结果。
- 发送前把当天整批任务预占状态提交到 `douyin-send-state` 分支的 `daily-state.json`。正式发送只在预占写入成功且读回确认后开始。
- 当天已有 `reserved`、`success` 或 `unknown`，整批跳过。修改好友、候选文字或重跑任务均不清空当天记录。部分好友失败后，剩余好友当天也不自动补发。
- 状态接口超时、缺失、损坏、冲突均停止；没有清空历史、自动重建或强制重试功能。不使用可能过期或被清理的 Actions Cache 作为发送凭据。
- 复用原项目新发出消息气泡及发送中/失败状态检查，再核对完整文字与目标身份。未知结果按 `unknown` 停止；不点击重试，不将按钮点击视为成功。
- 不上传截图、Trace、DOM、原始异常、Cookie、好友昵称或消息正文。日志只使用好友序号、时间及状态；状态分支仅记录日期、状态和 GitHub 运行编号。

## 最小配置

下面的原项目简易结构可用于首次 Dry Run，替换好友昵称。首次只填一位本人确认的正常联系好友。正式发送需改用下一段带 `sec_uid` 的 `targets` 配置。

```json
{
  "friends": ["好友A"],
  "messages": [{"type": "text", "value": "🔥"}],
  "timezone": "Asia/Shanghai",
  "prevent_duplicates": true,
  "target_open_retries": 0
}
```

正式发送配置（必须指定本人核对的稳定标识）：

```json
{
  "targets": [{
    "name": "好友A",
    "sec_uid": "替换为好友主页URL中user后面的实际标识",
    "messages": [
      {"type": "text", "value": "🔥"},
      {"type": "text", "value": "今天也开心"}
    ]
  }],
  "prevent_duplicates": true
}
```

不要照抄 `sec_uid` 占位文字。若网页既不提供可见个人主页链接，也无法通过唯一可见头像打开独立主页验证固定标识，本版会停止。同页跳转、多个主页标识、未知主页地址或标识不一致均停止。真实抖音页面尚未经过此环境的登录验证，不能据离线测试宣称页面一定兼容。

## 本地开发与检查（Windows PowerShell）

先安装 Python 3.11 或更高版本，并进入项目文件夹：

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock.txt
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m playwright install chromium
.\.venv\Scripts\python.exe -m compileall -q app scripts run.py
.\.venv\Scripts\python.exe -m pytest -q
```

本机可运行 `python run.py --dry-run`；需先设置本机 `DOUYIN_COOKIE` 与 `DOUYIN_CONFIG`，或放置未提交的 `config.json`。不要把 Secret 值写进命令历史。正式发送仅允许 GitHub Actions，并且需要 `--send` 和 `ENABLE_REAL_SEND=true` 两道显式开关。

测试与局限见 [TEST_REPORT.md](TEST_REPORT.md)，复用评估及安全边界见 [docs/REVIEW.md](docs/REVIEW.md)。`app/main.py`、多账号及通知模块保留供上游回归测试，部署入口只使用本版 `run.py`；旧 Docker/systemd/Windows 定时教程不适用于安全入口。上游原始说明见 `docs/UPSTREAM_README.md`，不要混用两套部署方式。

## 运行边界

免费推荐使用公开仓库的标准 `ubuntu-latest` Runner。私有仓库受账号免费额度限制，超额可能收费；本项目不使用大型 Runner、外部付费定时器或服务器。[GitHub 计费说明](https://docs.github.com/en/billing/concepts/product-billing/github-actions)

GitHub 定时可能延迟或丢弃高负载任务；公开仓库长期无活动也可能停用 schedule。Cookie 可能失效，云端网络可能触发安全验证，页面结构可能改变。都需要本人处理，无法保证火花不断或永久免维护。[GitHub 调度说明](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)

防重复覆盖本仓库这一账号的自动发送；无法可靠识别你在手机、其他仓库或其他工具里的手动发送。请同一账号只部署一套自动化。不绕过拉黑、隐私设置、验证码或平台风控。
