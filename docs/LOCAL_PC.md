# Windows 本机运行

本机模式使用 Windows 任务计划程序、Playwright 和 SQLite，不调用 GitHub Actions，也不需要 GitHub 凭据。保留每天北京时间07:30、三位好友加一个群和每日消息轮换。电脑关机或用户未登录时不能发送；当天07:30至23:30之间开机登录后补跑，过去日期不补发。

## 一次性配置

安装项目依赖和 Chromium，在项目 `.venv` 中运行。将已核验的四目标任务配置写入 `data/config.json`，完整 Playwright 登录态写入 `data/storage-state.json`。配置必须有 `prevent_duplicates: true`、`daily_message_rotation: true`，每个目标必须固定 sec_uid 或群会话身份，三位好友和一个群。登录态应保留 cookies、localStorage 和 IndexedDB。

`data` 含私人数据，不提交到任何仓库。Windows 上为这个文件夹限制当前用户和 SYSTEM 访问。不要将它放入项目发布包。

迁移时先停用旧的云端真实发送和临时恢复检查器，核对原持久记录，导入已处理日期，避免同日重复发送。完成后写入 `data/activation.json`：

```json
{"mode":"local","enabled":true}
```

先做零发送检查：

```powershell
.\.venv\Scripts\python.exe scripts\desktop_service.py
```

通过后安装任务：

```powershell
.\scripts\install_desktop.ps1
```

注册 `DouyinLocalDaily`，每天07:30、登录时、每15分钟检查一次。当天成功后检查不再打开浏览器。任务按当前用户运行，不保存 Windows 密码；不会自动唤醒关机电脑。

## 状态和维护

- `data/status.json`：最新日期、运行状态和检查时间。
- `data/daily.sqlite3`：当天批次预占、成功或未知状态，重启后保留。不要删除数据库来强制重发。
- `data/storage-state.json`：每次登录验证成功后原子保存的新会话，减少重复扫码。
- 当天发送前检查最多尝试三次。发送一旦预占或结果不明，当天禁止自动重试。部分失败可能意味着部分目标已成功，必须人工核验后再处理。
- 登录失效、验证码、名单缺失或发送不明时通过 Windows 消息提醒，同一天同一原因只提醒一次。没有登录用户时无法弹出提醒。
- 当前只保证核验本方消息是否成功发出；不保证抖音永远保留登录态或火花。

暂停：`Disable-ScheduledTask -TaskName DouyinLocalDaily`。恢复：`Enable-ScheduledTask -TaskName DouyinLocalDaily`。这些操作都保留当天发送记录。
