# Windows 11 初学者部署教程

本教程只使用你自己的 GitHub 与抖音账号。完成后由 GitHub 云端每日运行，本机不用挂机。全过程先保持正式发送关闭；只有你最后明确打开开关才允许发消息。首次建议只配置一个正常联系、同意接收消息的好友。

## 1. 获取代码并 Fork

1. 解压交付的 `douyin-auto-fire-safe.zip`，得到 `douyin-auto-fire-safe` 文件夹。不要把 Cookie 文件放进压缩包或上传到仓库。
2. 浏览器打开 [原项目](https://github.com/unmev/douyin-auto-fire)，登录自己的 GitHub，点击右上方 **Fork** → **Create a new fork**。
3. **Owner** 选自己的账号，仓库名保留 `douyin-auto-fire`，点击 **Create fork**。成功后地址应为 `https://github.com/你的用户名/douyin-auto-fire`。
4. 如已经 Fork，直接使用自己的仓库，不再新建第二套自动化。

## 2. 上传本版代码

需要 Git、GitHub CLI 和 Python 3.11+。在 Windows 终端 PowerShell 输入 `git --version`、`gh --version`、`py -3 --version` 检查。缺少时，分别从 [Git Windows 安装页](https://git-scm.com/install/windows)、[GitHub CLI](https://cli.github.com/)、[Python Windows 下载页](https://www.python.org/downloads/windows/) 安装；Python 安装器勾选 **Add python.exe to PATH**。安装后重新打开终端。

在解压的项目文件夹空白处右键 → **在终端中打开**，依次运行：

```powershell
gh auth login --web --git-protocol https --scopes workflow
gh auth setup-git
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\deploy.ps1 -Repository "你的用户名/douyin-auto-fire"
```

把最后一行的 `你的用户名` 换成自己的 GitHub 用户名。第一行按提示选择 GitHub.com，在浏览器输入终端提供的一次性代码并点击 **Authorize GitHub CLI**；只在 GitHub 官方网页操作。若以前登录的令牌失效，也按此重新登录。`workflow` 授权用于通过 Git 推送 `.github/workflows` 文件。

部署脚本会检查仓库属于当前登录用户、先把 `ENABLE_REAL_SEND` 设为 `false`、新建本地临时克隆、只复制交付清单内代码、移除两项不需要的上游工作流，再提交到你自己的默认分支。它不读取或上传本机凭证，不配置抖音 Secrets，不执行私信。若分支保护拒绝推送，提交保存在输出的本地克隆目录，按仓库规则提交 PR；不要强推。

成功判断：终端显示“部署成功”，自己仓库的 **Code** 页面能看到本版 README、`app/safe_main.py` 与 `.github/workflows/send.yml`；**Actions** 页能看到 **Safe Douyin Daily** 与 **Test**。首次 Fork 若 Actions 未启用，点击 **I understand my workflows, go ahead and enable them**。`Test` 变绿表示云端依赖与自动测试成功；没有配置 Secrets 时，定时 Dry Run 失败是正常的。

## 3. 本人在电脑上扫码获取凭证

在解压项目的 PowerShell 终端执行：

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock.txt
.\.venv\Scripts\python.exe -m playwright install chromium
.\.venv\Scripts\python.exe scripts\login.py
```

1. 自动打开 Chromium 和抖音官网后，用自己的抖音 App 扫码并在手机确认。平台要求验证时，由你本人完成。
2. 确认网页已经登录，回到终端按 **Enter**。脚本生成本机 `storage-state.json`。登录脚本只访问网页并保存登录状态，不发消息。
3. 再运行：

```powershell
.\.venv\Scripts\python.exe scripts\export_cookie.py
```

4. 成功时显示已生成 `douyin-cookie.json`。用记事本打开该文件，复制完整 JSON 数组，下一步直接放入 GitHub Secret。

Cookie 等同登录凭证。不要发给任何人、粘贴到聊天、提交 Git、放进截图或配置生成网站。`.gitignore` 已忽略登录状态及 Cookie 文件；部署脚本也仅复制文件清单，列表不含它们。完成配置后可删除本机 `storage-state.json` 与 `douyin-cookie.json`；凭证失效再本人扫码生成。

## 4. 配置两个 Secrets

打开 **自己的 Fork 仓库**，点击 **Settings** → 左侧 **Secrets and variables** → **Actions** → **Secrets** 页签 → **New repository secret**。

| Name | Secret 内容 |
| --- | --- |
| `DOUYIN_COOKIE` | 本机 `douyin-cookie.json` 的全部内容，即 `[{...}, {...}]`；不要填账号密码，也不是普通 `name=value;...` 字符串 |
| `DOUYIN_CONFIG` | 下方 JSON，先将 `好友A` 换成目标好友的精确昵称 |

每次分别填 **Name**、**Secret**，点击 **Add secret**。成功后列表显示两个名称，GitHub 不会再次展示原值。

```json
{
  "friends": ["好友A"],
  "messages": [{"type": "text", "value": "🔥"}],
  "timezone": "Asia/Shanghai",
  "prevent_duplicates": true,
  "target_open_retries": 0
}
```

支持添加好友或文字候选。`messages` 是候选池，每位好友每天只抽取一条；不会把整个数组连续发送。不要关闭 `prevent_duplicates`，安全入口会拒绝该配置。

**正式发送必须指定稳定身份：** 在自己的抖音网页打开目标好友个人主页，确认头像、昵称与联系人一致。复制地址 `/user/` 后、`?` 前的完整标识，作为 README 的 `targets[].sec_uid`；昵称也要保留。无法辨认实际标识时先只填精确昵称做 Dry Run，正式发送保持关闭；不要编造 UID。正式发送前把 `DOUYIN_CONFIG` 改成以下兼容上游 `targets` 的结构（替换昵称与 UID）：

```json
{
  "targets": [{
    "name": "好友A",
    "sec_uid": "替换为本人确认的实际主页标识",
    "messages": [{"type": "text", "value": "🔥"}]
  }],
  "prevent_duplicates": true
}
```

仅昵称的 `friends` 配置不允许正式发送，避免网页搜索分页漏掉同名联系人或昵称变化后误发。修改后重新运行 Dry Run。

GitHub 会给工作流自动提供短期 token，不需要第三个 PAT Secret。

## 5. 首次初始化持久状态

1. 自己仓库点击 **Actions** → 左侧 **Safe Douyin Daily** → 右侧 **Run workflow**。
2. **Use workflow from** 选择仓库默认分支，通常 `main`。
3. 保持 `dry_run` 勾选，并勾选 `initialize_state`，点击绿色 **Run workflow**。
4. 点击新运行记录 → **send** → **Run safely**。成功日志为“状态分支初始化成功；真实发送次数 0”。
5. 回到 **Code**，分支下拉菜单选择 `douyin-send-state`，可看到 `daily-state.json`。不要设它为默认分支，不要删除、编辑或回滚。

该步骤不启动浏览器，也不检查抖音凭证，只建立持久状态。它只做一次；重复勾选会失败并保护原历史。若创建分支成功但随后写文件失败，先保持发送关闭，核查运行日志与分支内容。不要简单删除分支重跑；必须本人确认从未发送并修复缺失文件，避免丢失去重证据。

如果报权限问题：仓库 **Settings** → **Actions** → **General**，核对 Actions 是否允许及组织是否限制 token 权限。发送 job 已声明 `contents: write`，这是更新状态所必需；组织或分支规则禁止机器人写入时会安全停止，不需要另建高权限 PAT。

## 6. 运行真正的 Dry Run

再次 **Actions** → **Safe Douyin Daily** → **Run workflow**：

- 保持 `dry_run` 勾选。
- **取消** `initialize_state`。
- 默认分支 → **Run workflow**。

成功日志包含“登录检查成功”“好友#1：唯一身份和聊天检查成功”“最终状态：dry_run_success；真实发送次数 0”。这次才验证真实云端登录和好友定位。任何一步失败都先处理，不要打开正式发送。

Dry Run 不写每日发送记录，可以反复运行；它可能打开聊天，但不会输入或发送预设私信。无法通过可见主页链接或唯一可见头像打开的独立主页核验身份、重复昵称、聊天窗口身份不同等情况会停止。抖音实际页面未在本次开发环境中登录验证，首次 Dry Run 是必要的部署验收。

## 7. 由你明确启用正式发送

**只有配置了本人核对的 `sec_uid`、确认该配置的 Dry Run 成功，且目标、内容正确后再进行。此步骤代表你明确允许真实发消息。**

1. 自己仓库 **Settings** → **Secrets and variables** → **Actions** → **Variables** 页签。
2. 找到 `ENABLE_REAL_SEND` → 编辑；没有则 **New repository variable**。
3. **Name** 填 `ENABLE_REAL_SEND`，**Value** 填小写 `true`，点击 **Update variable** / **Add variable**。
4. 首次可手动验收一次：**Actions** → **Safe Douyin Daily** → **Run workflow**，取消 `dry_run`，保持 `initialize_state` 未勾选，点击 **Run workflow**。这一次会发真实消息。
5. 去自己的抖音聊天确认只向配置好友发了一条。日志应为“发送成功（新消息及页面发送状态已确认）”和最终 `success`，状态分支当天记录为 `success`。

随后再次手动或当晚定时运行，会显示 `duplicate` 并跳过。这是防重复生效。若结果是 `unknown` 或仍为 `reserved`，当天不要删历史补跑；去抖音自行核实，系统当天保持停止。

## 8. 每天自动运行与暂停

工作流已包含每日 **Asia/Shanghai 20:00** 的原生 schedule，无需外部 cron。代码在默认分支、Actions 已启用、两个 Secrets 有效、状态初始化成功且变量为 `true` 时，下一次定时才会尝试真实发送。变量为 `false` 或不存在时，定时运行只做 Dry Run。

查看历史：仓库 **Actions** → **Safe Douyin Daily** → 对应运行 → **send** → **Run safely**。`unknown` 是失败退出，不会显示成成功；`duplicate` 是安全跳过，可显示绿色。只有页面确认过的新消息才记录为成功。

暂停发送：将 `ENABLE_REAL_SEND` 改回 `false`。彻底暂停所有检查：**Actions** → **Safe Douyin Daily** → 右上角 **…** → **Disable workflow**；恢复时点 **Enable workflow**。不要删除状态分支。修改变量不会撤回已经开始的发送；需要立即停机时可在当前运行点 **Cancel workflow**，已有预占仍会阻止当天重试。

GitHub schedule 并不保证 20:00 整执行，高负载时可能延迟甚至丢弃；公开仓库连续 60 天无活动可能自动停用定时，请定期检查 Actions 状态。本版正常发送会写入状态分支，但不承诺这能排除所有自动停用情形。[GitHub 官方限制](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)

## 常见问题

| 现象 | 处理方法 |
| --- | --- |
| 没有 Run workflow | 确认进入自己的仓库、工作流文件在默认分支、已启用 Actions |
| 配置错误 | 核对两个 Secret 名称；Cookie 必须是 JSON 数组，配置是 JSON 对象，双引号/逗号正确，不能填示例 UID |
| 登录失效 | 本机重新运行扫码与导出脚本；在 Secret 列表点击 Cookie 右侧编辑按钮，填新值 → Update secret；再跑 Dry Run |
| 需要安全验证 | 本人正常登录处理；云端仍触发时停止自动化，不破解、不绕过验证 |
| 好友或页面控件无法确认 | 在自己网页核对精确昵称、主页 UID；检查是否有同名或群聊。实际 DOM 不兼容时需要适配代码，不要放宽匹配规则 |
| 状态接口失败 | 检查 `douyin-send-state/daily-state.json`、Actions 写权限、仓库规则和 GitHub 可用性；不要删除历史重试 |
| `duplicate` | 当天整批已经预占/处理；手动与定时共享记录，正常跳过 |
| `unknown`、`reserved` | 当天发送结果不明或任务被中断；去抖音本人核实，系统当天不再自动补发，剩余好友也不补发 |
| 修改好友后当天未发送 | 当天整批锁定是安全设计，新配置从次日生效；Dry Run 仍可验证新配置 |
| 有草稿或消息失败 | 清理由本人确认的草稿或检查正常私信权限后，次日重试；不点击平台“重试”按钮 |
| Windows py 命令找不到 | 安装 Python 3.11+ 与 Python Launcher，重新开终端；或将命令里的 `py -3` 换为本机 Python 的完整路径 |
| Actions 长期没运行 | 查看是否停用、默认分支是否正确、GitHub 状态、账号配额；恢复后先 Dry Run |

推荐免费方式是公开 Fork + 标准 Ubuntu Runner；公开代码不等于公开 Secret。私有仓库有免费额度和超额计费，请在账号 **Settings → Billing and licensing** 核查自己的额度与预算。[GitHub 计费说明](https://docs.github.com/en/billing/concepts/product-billing/github-actions)

不要同时在第二个仓库、电脑定时任务或其他平台部署同一账号。手机手动发送及其他工具不在本仓库去重范围内。Cookie 失效、平台限制或页面变化仍需要本人维护，无法保证火花不断。
