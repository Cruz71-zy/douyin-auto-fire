# 复用评估与实现边界

核查日期：2026-10-17。上游仓库 `https://github.com/unmev/douyin-auto-fire`，基准提交 `af0035f764d3a1adac5884bc3d73adcf031a3dd6`，该提交日期 2026-09-11。

| 项目 | 上游现状 | 本版处理 |
| --- | --- | --- |
| 许可证 | PolyForm Noncommercial 1.0.0，保留版权通知 | 保留 LICENSE，个人非商业使用；并非无限制商用许可 |
| 登录与 Chromium | `app/browser.py`，支持 Cookie / storage state | 复用浏览器生命周期、Cookie 归一化与登录检测；入口仅接受抖音域名 Cookie，关闭 Trace |
| 配置 | `app/config.py`、`models.py`，兼容 friends/targets、候选与多类型消息 | 复用解析器，附加单账号、最多十人、仅文字、强制上海日期与防重复的校验 |
| 搜索 | `app/douyin.py` 支持多个页面选择器，也有首个匹配、群名后缀及隐藏标题回退 | 继承搜索流程，安全适配器仅接受唯一可见精确昵称、唯一个人主页标识及一致的聊天身份；无链接时仅允许点击唯一可见头像，通过独立主页 URL 精确核验 UID |
| 发消息 | `app/sender.py` 检测新气泡及失败/等待标识，部分贴纸逻辑会重试 | 复用文字确认底层函数；安全入口不调用贴纸或重试逻辑，拒绝已有草稿，触发前复核身份及日期 |
| 重复发送 | 本地 history；键包含消息和任务，缺失/变化可能重新发 | 新增 GitHub Contents API SHA 条件更新，预占每日整批；键仅按日期，配置变更不解锁 |
| Actions | schedule 原本注释，dispatch 默认 false，失败上传 artifacts | 固定上海 20:00；默认 Dry Run；固定并发组；无截图/Trace/构建产物上传；依赖和 Action 版本锁定 |
| 测试 | 216 项回归测试，包括 Chromium 页面模拟 | 保留回归；旧入口测试改为导入保留的 `app.upstream_entry`；增加安全单元及实际 Chromium 模拟测试 |
| 其他部署 | Docker、多账号、Webhook 等 | 保留库代码以利回归；移出 Docker 发布与外部同步工作流，避免 Fork 后额外执行或重复触发 |

这是按个人使用目标缩小范围的改造。已有解析器、选择器、浏览器和消息确认可以复用；原本持久状态不足以承担跨 Runner 的安全去重，所以新增少量标准库代码，未引入数据库或外部服务。

## 持久状态协议

首次通过手动工作流勾选 `initialize_state` 并保持 `dry_run` 完成初始化。创建 `douyin-send-state` 分支；分支已存在则创建失败，不重置历史。初始化运行不启动浏览器、不发送消息。

正式任务先读取状态；当天已有记录就跳过。完成所有好友的身份预检后，用旧文件 SHA 提交当天 `reserved`，再读回检查写入结果。只有成功返回才允许触发真实发送。发生并发写冲突或提交超时，当前运行停止且不重试。

所有好友通过页面确认后，状态改为 `success`；预占后发生异常则尽力改为 `unknown`。即使更新失败或进程被强制终止，已经持久保存的 `reserved` 仍阻止当天后续任务。整批锁定意味着一个好友失败会使剩余好友当天不再补发，这是有意的保守取舍。

状态仅保留日期、结果和 GitHub run ID，不含好友、消息或凭证。状态保存在 Git 历史中，不随 Runner 销毁，也不依赖 Cache/Artifact 的保留期。不存在协议允许的删除历史或重置同日状态；仓库拥有者手动删除分支、修改文件、回滚提交或另建自动化可能破坏该保证。

## 权限与隐私

工作流整体为 `contents: read`，发送 job 需要唯一额外权限 `contents: write` 写状态。使用 GitHub 自带短期 `github.token`，不要求额外 PAT；checkout 不持久保存 token。状态 API 使用固定 github.com 地址，禁止 HTTP 重定向，不输出响应正文或异常堆栈。Secret 只在最终运行步骤作为环境数据读取，不拼接入 shell。

日志只输出固定错误分类及好友序号，便于初学者定位配置、登录、安全验证、页面匹配或状态存储问题。更详细的网页调试须由用户在自己的环境查看；本版不保存敏感诊断产物。受信任仓库管理员仍有能力修改代码读取 Secrets，因此不要给不可信人员仓库写权限。

## 无法在离线环境证明的事项

1. 真实抖音 DOM 是否仍含本版要求的可见个人主页链接及标题；缺少任何证据就停止，需要实际 Dry Run 验证。
2. 云端 Cookie 能否持续登录、是否触发平台安全验证。
3. GitHub 实际 Actions 接受、权限、API 写入与调度；本地只校验 YAML 和协议模拟，部署后须运行一次初始化和 Dry Run。
4. 页面气泡和状态是网页证据，不等于接收方已读、服务器提供的事务回执或火花必然延续。平台后续异步变化不可能完全排除。
5. 昵称搜索结果可能被网页分页/虚拟化限制；正式发送强制填写本人核对的 `sec_uid`，仅昵称结构用于 Dry Run，避免把当前可见结果唯一误认为全站唯一。即使配置 UID，网页缺少对应身份信息时仍停止。

官方依据：

- [GitHub schedule 与 timezone](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax#onschedule)
- [GitHub Contents API 与 sha 条件更新](https://docs.github.com/en/rest/repos/contents#create-or-update-file-contents)
- [GitHub 定时任务限制](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)
- [上游代码和许可证](https://github.com/unmev/douyin-auto-fire/tree/af0035f764d3a1adac5884bc3d73adcf031a3dd6)
