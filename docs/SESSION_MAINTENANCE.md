# 完整登录状态与日常维护

当前修复针对 Safe Douyin Daily 安全入口。默认仍为 Dry Run，原有好友身份核验、持久防重复、发送开关和失败即停止规则保持生效。

## 本次故障与修复范围

2026-10-24 北京时间 00:41 的运行在 preflight 阶段出现 AuthenticationError：私信地址存在、登录提示可见、搜索框不存在、安全验证提示未见。前一天日志相同。这证明当时未通过私信登录检查，不能仅凭这些信息区分 Cookie 过期、不完整或平台拒绝当前运行环境。

- [失败运行](https://github.com/Cruz71-zy/douyin-auto-fire/actions/runs/39987471474)
- [前一天运行](https://github.com/Cruz71-zy/douyin-auto-fire/actions/runs/39815983737)

代码还存在独立缺陷：通用浏览器层支持完整 storage state，但安全入口强制清空 storage_state，且现有工作流只传入 Cookie Secret。修复后支持 DOUYIN_STORAGE_STATE，保留 Cookie、localStorage 和 IndexedDB；未配置时才使用旧 Cookie。显式配置的状态无效时会停止，不会改用旧 Cookie。启用加密会话存储后，日常运行优先加载已经保存的最新加密会话，初始 Secret 用于首次启动或本人明确发起的重新登录更新。

## 先恢复登录

在自己的电脑、仓库目录执行：

```powershell
python -m pip install -r requirements-auth.txt
python -m playwright install chromium
python scripts/sync_auth.py --login --verify
```

先确保本机 Git Credential Manager 已授权目标 GitHub 账户。同步工具以非交互方式读取本机现有凭据，只在内存捕获，不显示令牌，也不把令牌写入仓库；无可用凭据时停止。默认目标仓库为 Cruz71-zy/douyin-auto-fire，可用 --repo owner/repository 明确指定其他仓库。

工具先检查仓库写权限、已有发送日记以及正在运行和排队的 Safe Douyin Daily。通过后复用本机已有登录，失效时由本人扫码，完成后回终端按 Enter。私信页面登录、风控提示和搜索框检查成功后才保存 storage-state.json，不搜索好友、不编辑或发送消息。保存包含 IndexedDB，剔除非抖音域数据，用同目录临时文件原子替换。失败保留原文件；已有文件损坏时允许交互重新登录。

若仍出现安全验证或私信页面不能打开，先由本人在平台完成验证；工具不绕过验证。

完整登录状态可能超过 GitHub 单个 Secret 的 48 KB 限制，不能将整个文件直接粘贴到 Secret。sync_auth.py 自动准备两种数据：完整状态加密后上传 auth-state.enc；Secret 只存小型引导状态。首次配置和后续本人重新登录均使用上述命令，工具加密更新两个 Repository Secret：

- DOUYIN_STORAGE_STATE：仅包含 Cookie 和空 origins 数组的引导状态，保存前检查小于 48 KB。完整 localStorage 和 IndexedDB 留在加密文件中。
- DOUYIN_SESSION_KEY：本次同步由 Fernet.generate_key() 随机生成的 256 位密钥材料，使用 URL 安全 Base64 编码。只通过 GitHub 公钥的 SealedBox 加密上传 Secret，不输出、落盘或放入密文文件。

本机完整状态仍是敏感明文，不提交到公开分支、状态分支、日志、Artifacts 或聊天。默认文件和临时文件已被 .gitignore 排除；使用自定义 --state 路径时自行确保该文件也被忽略。每次明确执行本机同步都会生成新密钥并替换对应密文，无需读取或保存旧密钥。日常 Actions 运行沿用当前密钥，不自行轮换。

工作流需传入这两个 Secret，完整变更见交付补丁。状态分支 douyin-send-state 和有效的 daily-state.json 必须已存在，同步工具不会创建或重建发送日记。若 Safe Douyin Daily 正在运行或排队，同步停止；首次写入前还会再次检查。密文、密钥 Secret、引导 Secret 分多次 API 写入，无法作为一个原子操作提交；同步中断时可能暂不匹配，运行会停止而不发信。待工作流空闲后重新执行同步，不删除发送日记。

--verify 最后仅请求 send.yml 的 Dry Run：dry_run=true、send_once=false、refresh_session=false。请求成功不代表验证已经通过，需查看 Actions 的 dry_run_success。工具不修改 ENABLE_REAL_SEND 等日常变量。更新配置不会修复尚未应用的旧入口；平台拒绝跨环境会话时，云端仍可能要求重新登录，须在实际运行环境验证。

## 加密保存刷新后的会话

配置 DOUYIN_SESSION_KEY 后，完整登录状态使用 Fernet 加密，密文保存在已有 douyin-send-state 分支的 auth-state.enc，封套绑定仓库名称和格式版本。Cookie、localStorage、IndexedDB 的明文和加密密钥都不写入该分支。

每次运行先读取并验证现有密文；只有文件明确返回 HTTP 404、且已有发送日记读取校验成功，才允许首次使用 DOUYIN_STORAGE_STATE 启动。打开私信页面并通过登录和搜索框检查后，捕获平台实际返回的完整会话，加密保存一次，然后继续好友检查和后续发送流程。保存使用当前文件 SHA 做 CAS 更新；版本变化、密文损坏、密钥不匹配或接口结果不确定时立即停止，不自动重试覆盖，也不重建或删除 daily-state.json。

日常 GitHub API 使用本次 Actions 运行提供的短期 GH_STATE_TOKEN。仅本机显式同步使用 Git Credential Manager 的既有授权；无需把长效 GitHub 令牌保存到仓库或新增 Secret。未配置加密密钥时兼容原有静态 Secret 模式，不会启用密文持久化。

## 会话失效后的更新

出现 auth_required 后，由本人在本机重新运行：

```powershell
python scripts/sync_auth.py --login --verify
```

命令完成扫码恢复、完整会话加密同步、密钥轮换和 Cookie 引导 Secret 更新，再请求 Dry Run。确认 dry_run_success 后，后续定时任务继续使用最新完整会话。不要只更新 Cookie 引导 Secret 来替换已有完整密文，也不要把大文件直接粘贴到 Secret。

refresh_session 输入保留，默认 false，且只允许手动 Dry Run；它会用 Secret 替换本轮登录来源。当前 Secret 是 Cookie-only 引导状态，完整状态更新应使用 sync_auth.py，正常验证保持 refresh_session=false，避免主动丢弃完整的加密会话。

如需验收本轮真实发送，需显式手动选择 send_once 并取消 dry_run。send_once 默认为 false，仅手动事件生效，且仍须通过好友固定身份、发送状态和当天防重复检查。仓库变量 ENABLE_REAL_SEND 当前保持 false；本次手动选择不会永久开启日常真实发送。生产启用与实际发送验收仍在进行中，此文档不代表已上线或已成功发信。

## 持续维护的边界

本机可运行以下命令复用保存的会话，并在私信登录检查成功后保存服务器返回的最新状态：

```powershell
python scripts/login.py --check-only
```

此命令无界面、不扫码、不发信；会话失效时返回 2，保留原文件并要求本人重新登录。它保存平台实际返回的状态，不保证平台延长会话有效期。尚未安装任何本机计划任务或后台常驻服务。

GitHub 托管 runner 每次重建，引导 Secret 是静态快照，日常运行后不会自动更新 Secret。加密会话存储保存每次登录检查成功后平台实际返回的完整状态，使后续任务可以复用；它不能保证平台延长会话有效期。auth_required 仍需要本人扫码并执行本机安全同步。修复会在 Actions Summary 给出维护步骤；不会在认证失败后自动重试发送。发送状态未知仍锁定当天，切勿删除 daily-state.json 或重建状态分支来强行重试。

加密保存会话能减少重复搬运凭证的操作，不能替代本人处理平台注销和安全验证，也不承诺永久免登录。只有本人显式执行 sync_auth.py 才更新两个 GitHub Secret；日常任务不改 Secret、不绕过平台验证。本次尚未安装本机后台常驻服务或永久开启日常真实发送。

公开仓库的定时工作流在 60 天无仓库活动时可能被 GitHub 停用；定时执行也可能延迟。因此长期运行还需检查调度状态。参见 [GitHub schedule 文档](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)。

## 运行环境调整

补丁建议将 ubuntu-latest 固定为 ubuntu-24.04，并仅安装 headless shell，减少系统镜像升级影响及多余 Chromium 下载。本次约 10 分钟耗时主要在 apt 安装系统依赖；这些改动尚未在 GitHub 上验证，不能保证消除镜像站网络延迟。没有增加自动重试或额外发送时段。

参考：[Playwright 认证状态](https://playwright.dev/python/docs/auth)、[仅安装 headless shell](https://playwright.dev/python/docs/browsers#chromium-headless-shell)。

## 变更清单

- app/auth_state.py：抖音域校验、完整状态规范化、原子文件保存。
- app/session_store.py：Fernet 加密会话、已有状态分支上的 CAS 更新和错误时停止。
- app/safe_main.py：最新加密状态优先加载、验证成功后保存刷新态、手动 Dry Run 更新、认证失效的维护提示与 Actions Summary。
- scripts/login.py：扫码恢复、私信页验证、--check-only 本机状态更新。
- scripts/sync_auth.py、requirements-auth.txt：本机已有 GCM 授权、完整状态密文 CAS、SealedBox 加密 Secret 与 Dry Run 请求。
- .gitignore：忽略临时凭证文件。
- tests/test_auth_state.py、tests/test_login_state.py、tests/test_auth_browser.py、tests/test_session_store.py、tests/test_session_integration.py、tests/test_sync_auth.py：虚构凭证、本地网页、加密存储、Secret 容量和发送前置顺序的回归验证。

补丁基于 main 的 b84f1dd6a07c51c0e2ae72c1bb563c8ab3fe1522。真实登录恢复和生产调度需要应用补丁后由本人扫码验收；离线测试不能替代实际账户验证。
