# 抖音续火花：Windows 本机自动运行

在 Windows 电脑开机并登录后运行，默认每天北京时间07:30。当天错过计划时自动补跑，电脑关闭时不发送。运行不依赖 GitHub Actions；GitHub 仅用于托管代码。

基于 [unmev/douyin-auto-fire](https://github.com/unmev/douyin-auto-fire) 二次开发，保留原作者版权及 [PolyForm Noncommercial 1.0.0](LICENSE) 许可证。

## 已实现

- 三位好友和一个群，使用固定身份核验，发送前再次确认聊天目标。
- 每日消息轮换，每个目标每天最多一条；所有目标预检通过后才开始发送。
- Windows 任务计划程序：07:30、登录后补跑、每15分钟检查。当天成功后安静跳过。
- SQLite 持久记录，原子预占，重启保留；发送结果未知时当天停止自动重试。
- 登录态保存 cookies、localStorage、IndexedDB，检查成功后原子更新。
- 登录失效、验证码、配置不足或发送不明时提醒，同日同原因去重。
- 默认命令只做零发送验证。私密名单、登录态和数据库位于忽略提交的 data 目录。

## 开始使用

阅读 [本机部署与维护说明](docs/LOCAL_PC.md)。首次需要配置并核验目标、本人登录一次；登录被平台注销或要求验证码时仍需本人处理。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock.txt
.\.venv\Scripts\python.exe -m playwright install chromium
.\.venv\Scripts\python.exe scripts\desktop_service.py
.\scripts\install_desktop.ps1
```

配置未完成时服务停止并提醒，不发送任何消息。若迁移时本机已保存两个好友和一个群，可用 `scripts/add_desktop_friend.py --name 昵称` 唯一核验并补齐第四位好友；此工具只验证不发送。

不要同时启用旧云端发送。迁移应暂停旧任务并导入旧发送记录。旧方案文档留在 [历史 GitHub Actions 说明](docs/LEGACY_GITHUB.md)，不代表当前运行方式。

自动化只能核验本方消息发送结果，不能保证账号永不掉线、平台规则不变或火花必定保留。
