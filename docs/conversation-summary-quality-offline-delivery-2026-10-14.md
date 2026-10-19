# 会话摘要测试实施：离线阶段交付

日期：2026-10-14。依据 `docs/conversation-summary-quality-test-plan-v2-2026-10-14.md`。

用户本轮选择“只先实施并运行离线测试”。因此本次交付是P0离线测试基础设施、机制验证和两项有离线复现证据的确定性修复；不是完整真实模型质量评测，也没有重启或切换正式Web服务。

## 1. 已实施

| 内容 | 实现 |
| --- | --- |
| 固定虚构语料 | `evals/summary_quality_cases_v2.json`，9类场景，金标准与源消息本地编号映射；不导入正式聊天 |
| 独立运行器 | `scripts/evaluate_summary_quality.py`，真实摘要服务/仓库/模型网关搭配脚本响应，临时数据库，默认无网络 |
| 来源材料边界 | 只给摘要用户文本与上一版条目；验证助手猜测不进入摘要请求 |
| 三组输入捕获 | 通过现有 `TravelAgent` 与 `ModelGateway`，在脚本客户端边界捕获完整原文、仅摘要、无摘要的实际请求 |
| 可选诊断 | 摘要服务与上下文构建支持诊断回调，默认关闭；记录状态、版本、覆盖、大小、哈希、失败类别，不记录正文 |
| 只读检查 | `scripts/inspect_conversation_summary.py`，复用只读连接，绕过仓库初始化迁移，不创建缺失数据库 |
| 新增回归 | `tests/test_summary_quality_contracts.py`，22项，包括原文泄漏、跨摘要更新、分批、渲染、故障、隔离和只读安全 |

没有新增强制摘要按钮、公开诊断接口、正式数据库写入或生产触发阈值覆盖。诊断回调目前用于隔离测试，未自动接入所有生产会话；完整Web实际注入采集仍属于后续阶段。

## 2. 离线复现后修复的问题

### 2.1 完整消息边界的分批接续

修复前，约39073个用户字符的Q07材料首次处理后只覆盖到数据库ID4（目标ID9）。因正好停在完整消息边界，`progress_json`为空；剩余5轮达不到8轮阈值，末尾的未决事项没有被处理。

修复：达到输入上限时记录下一条未处理消息ID与偏移，即使偏移是0也保留接续标记。它仅延续已经达到正常触发条件的批次，不降低首次8轮/9000字符阈值。

复测：Q07两次脚本摘要调用，版本从1到2，覆盖到目标ID9，末尾事实进入条目，进度清空。

### 2.2 带引用摘要的原子渲染

修复前，条目文字1776字符满足上限，但附上引用后为1895字符，直接截到1800字符会产生半条摘要或半个引用。

修复：新增 `core/conversation_summary.py`，仓库和上下文共用完整条目渲染；注入预算包含引用与换行，不能从条目内部截取。超预算时只保留完整前缀条目并报告内容被缩减。

这避免格式/语义片段被截断，但不保证超预算的最后一项一定保留；重要性选择仍需后续真实摘要质量评测。没有扩大1800字符预算，没有更改摘要提示词或格式版本。

## 3. 测试结果及边界

- 新增专项22/22通过；既有摘要专项3/3、生命周期10/10通过，共35项。
- Q01—Q07的脚本响应可以通过触发、来源验证、发布及覆盖流程；Q09九种故障/隔离检查通过。
- Q01三组请求已捕获，摘要以外的历史通道没有夹带选定早期事实；无摘要请求不含《慢慢看桥》。各回答都是脚本占位回复，不能用于质量打分。
- Q06用户材料13216字符，不靠助手长回复达到阈值；Q07约39073用户字符验证分批处理。
- 22条手工发送模板不命中当前本地实时查询分类或摘要排除规则；这不是前置语义模型的真实验证。
- 生产装配离线回归120/120通过。该报告的221次模型调用是脚本模拟调用，不是付费API请求。
- 全量后端冻结版回归829/829通过（Python 3.11.15，452.719秒，生产源码及测试源码在运行前后一致）。其后评测脚本统一了三组请求的来源说明与时间元数据，专项及评测再次复查；没有再改动生产或测试源码。

报告明确记录 `real_model_calls=0`、`real_model_quality=null`。脚本理想条目来自金标准，仅用于验证机制；绝不能据此宣称真实模型事实召回率100%、稳定性合格或节费收益。

## 4. 当前仍未通过的语义反例

Q08共6条校验反例，金额错误与无效来源被拒绝，但以下4项仍未达到预期：

| 反例 | 当前表现 | 风险 |
| --- | --- | --- |
| 正确引用配错误计价单位 | 每间每晚被反写为每人每天仍能通过 | 错误结论被接受 |
| 否定对象反转 | “不是父母，是姨妈”反写仍能通过 | 错误结论被接受 |
| 人民币/CNY等价表达 | 正确CNY表达被拒绝 | 正确摘要无法发布 |
| 两个月/2个月等价表达 | 正确数字表达被拒绝 | 正确摘要无法发布 |

本轮没有偷偷修改这些金标准，也没有为了通过少量脚本反例重写通用自然语言校验。它们在报告中以 `known_guard_failures` 列出；严格校验模式退出码为1，表示语义准入未通过。

后续P1仍需要处理提示词职责、单位/否定/状态核验与等价表达，并验证真实模型会生成什么样的条目。当前不能把整体摘要功能评为“质量合格”。

## 5. 可重跑命令

在项目目录运行；这些命令不读取模型密钥、不调用模型或地图，不访问正式数据库：

```powershell
cd E:\Agent\travel_agent
& D:\anaconda3\envs\agent\python.exe -X utf8 scripts/evaluate_summary_quality.py --output evals/summary-quality-offline-recheck.json
```

正常模式退出码判断机制运行，输出仍明确列出语义缺陷。若要求语义反例也全部通过：

```powershell
& D:\anaconda3\envs\agent\python.exe -X utf8 scripts/evaluate_summary_quality.py --ids Q08 --strict-guards --output evals/summary-quality-guards-recheck.json
```

当前第二条命令应退出1，这是报告已知缺陷，不是网络故障。运行器本轮只有离线模式，没有隐藏的真实模型调用选项。

专项回归：

```powershell
& D:\anaconda3\envs\agent\python.exe -m unittest discover -s tests -p test_summary_quality_contracts.py -q
& D:\anaconda3\envs\agent\python.exe -m unittest discover -s tests -p test_conversation_summary.py -q
& D:\anaconda3\envs\agent\python.exe -m unittest discover -s tests -p test_v3_summary_lifecycle.py -q
```

只读查看一条指定会话的摘要，需替换真实会话存储scope和owner；该命令默认不输出正文：

```powershell
& D:\anaconda3\envs\agent\python.exe -X utf8 scripts/inspect_conversation_summary.py --database data/web/travel_bot.db --scope "web:会话ID" --owner "用户ID"
```

显式加 `--include-content` 才输出所选摘要正文和引用。它验证来源与依赖，不等于运行时生命周期可用、实际模型注入或语义质量合格。本次只在临时数据库验证此工具，没有读取正式摘要正文。

## 6. 证据文件

- `evals/summary-quality-source-baseline-2026-10-14.json`：改动前源码哈希和确定性反例。
- `evals/summary-quality-offline-before-2026-10-14.json`：修复前分批停滞及其他场景。
- `evals/summary-quality-offline-after-2026-10-14.json`：修复后各阶段、来源映射、摘要快照和三组请求。
- `evals/summary-quality-guard-check-2026-10-14.json`：仍未通过的语义反例。
- `evals/summary-quality-backend-2026-10-14.json`：全量后端回归。
- `evals/summary-quality-production-offline-2026-10-14.json`：生产装配脚本模型回归。

前后报告的语料相同，可通过 `corpus_hash` 核对。报告正文只包含本次固定虚构材料；不包含密钥或正式用户健康资料。

## 7. 部署状态与后续

代码和离线工具已落地到项目工作区。本轮没有重启正式服务、启用生产摘要开关或更换模型；正在运行的旧进程不会自动加载这些Python改动。

如果后续要验收Web，请在无重要任务时按原启动方式重新加载后端，并按v2方案使用专用虚构会话。真实模型测试、P1语义改进前后对照、P2多模型/档位稳定性、完整Web实际调用验证尚未执行，须再确认模型调用预算。
