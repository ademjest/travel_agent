# GitHub Actions QQ 登录重试与运行可见性设计

## 背景

`Scheduled QQ Bot` 在 GitHub 托管 Runner 上启动时，可能在
`qq-botpy` 请求 `https://bots.qq.com/app/getAppAccessToken` 的连接阶段
超时。`qq-botpy 1.2.1` 将该请求的总超时固定为 20 秒且没有重试，
因此一次临时网络故障就会让整个 Workflow 失败。

此前注释网络诊断步骤不会改善连接，只会移除判断 IPv4、IPv6、DNS
或 GitHub 出口路由问题所需的日志。Bot 在线但没有收到消息时也几乎
不输出内容，容易让 Actions 页面看起来像没有运行进度。

## 目标

- 保留不泄露 Secrets 的 QQ 网络连通性诊断。
- Bot 首次登录失败时自动重试，降低临时网络抖动导致的失败率。
- Bot 空闲期间定期输出心跳，让 Actions 页面持续显示运行状态。
- 保留现有运行时长校验、定时停止、状态加密和缓存保存行为。

## 非目标

- 不修改 `qq-botpy` 源码或在运行时 monkey patch SDK。
- 不强制 IPv4 或 IPv6；在获得诊断证据前不猜测地址族问题。
- 不把 GitHub Actions 改造成永久在线服务。
- 不改变 QQ、LLM、高德 Secrets 或业务代码。

## Workflow 设计

### 网络诊断

恢复独立的 `Diagnose QQ connectivity` 步骤：

- 输出 `qq-botpy`、`aiohttp` 和 `aiohappyeyeballs` 版本。
- 输出 `bots.qq.com` 的地址解析结果。
- 分别执行 IPv4 和 IPv6 HTTPS 连接检查。
- 每次检查使用较短的连接和总超时。
- 所有诊断命令允许失败，不能阻止 Bot 启动。
- 不发送真实 AppID 或 Secret。

该步骤的日志与 `Run QQ Bot` 分开显示。诊断结束后，用户需要在
GitHub Actions 页面展开 `Run QQ Bot` 查看 Bot 日志。

### Bot 启动重试

`Run QQ Bot` 最多启动三次独立 Python 进程：

1. 第一次失败后等待 30 秒。
2. 第二次失败后等待 60 秒。
3. 第三次失败后保留最后一次退出状态并让步骤失败。

每次启动都显示当前尝试次数。新的 Python 进程会重新创建
`qq-botpy` 客户端和网络会话，避免复用失败状态。

当 `timeout` 返回 124 或 130 时，表示 Bot 达到计划停止时间，Workflow
按成功处理，不再重试。进程正常返回 0 时也保持现有成功语义。

### 心跳日志

每次 Python 进程运行期间，同时启动一个仅负责输出日志的后台心跳：

- 每 60 秒输出一次 Bot 进程仍在运行的信息和当前尝试次数。
- Python 进程结束后立即停止并回收心跳进程。
- 心跳不访问网络、不读取 Secrets，也不影响 Bot 退出状态。

心跳只证明 Bot 进程仍存在，不宣称 QQ WebSocket 或 Token 一定健康。

## 错误处理

- 诊断失败：记录结果并继续启动。
- 登录或运行异常退出：若仍有次数则等待后重试。
- 计划时长到期：成功结束。
- 三次均异常退出：输出最终状态并失败。
- 无论 Bot 成功或失败，现有 `if: always()` 状态加密步骤继续执行。

## 验证

- 更新部署配置测试，确认诊断步骤处于启用状态。
- 测试确认 Workflow 包含三次尝试、30/60 秒退避和 60 秒心跳。
- 测试确认原有 `timeout`、124/130 正常结束以及状态保存条件仍存在。
- 运行 `tests.test_deployment_config`。
- 运行完整 `python -m unittest discover -s tests -v` 回归。
- 运行 `git diff --check`。

## 剩余风险

重试只能缓解临时网络故障。如果多次 Action 的 IPv4 和 IPv6 诊断都
无法连接 QQ，而其他依赖安装和测试正常，则问题更可能是 QQ 对 GitHub
托管 Runner 出口的路由或 IP 可达性限制。届时应改用亚洲或国内 VPS、
自托管 Runner 等更稳定的运行环境，而不是继续增加重试次数。
