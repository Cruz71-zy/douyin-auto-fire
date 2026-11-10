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
- 新版使用逐目标回执表，区分待发送、发送准备、已跨越触发边界、确认成功和结果不明。发送前先持久标记，再触发消息；当天已确认成功的目标不会重发。
- 明确未跨越发送触发边界的失败可自动补救，每次检查最多再核验一次，并受每日尝试上限约束；其他未发送目标可继续处理。未知目标保留记录并提醒，不盲目重发。原有整批unknown但没有逐目标证据的历史记录不会自动转换成“未发送”。
- `data/service.log`保留本机执行阶段，文件达到约1MB后滚动，帮助定位中断点；日志不输出昵称、固定身份或消息正文。
- 登录失效、验证码、名单缺失或发送不明时通过 Windows 消息提醒，同一天同一原因只提醒一次。没有登录用户时无法弹出提醒。
- 当前只保证核验本方消息是否成功发出；不保证抖音永远保留登录态或火花。

暂停：`Disable-ScheduledTask -TaskName DouyinLocalDaily`。恢复：`Enable-ScheduledTask -TaskName DouyinLocalDaily`。这些操作都保留当天发送记录。
