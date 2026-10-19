# 会话摘要质量 v4 升级实施总结

日期：2026-10-15。依据用户批准的 `summary-quality-improvement-analysis-2026-10-14.md`，其中摘要system以用户重新润色的文档为准。

## 1. 结果与状态

代码与离线验证已实施；2026-10-15 17:31经用户批准，已在队列空闲后正常重启正式Web后端并完成健康检查。真实模型质量复测仍未追加，不能用离线通过或启动成功冒充线上质量通过。

部署结果：旧进程33296通过定向Ctrl+Break正常退出，新进程24548在原 `http://127.0.0.1:8080` 提供服务；live/ready均200，五个后台任务正常。数据库已备份并只新增prompt_hash，完整性检查ok；主要业务记录数量保持一致，模型调用记录93→93，没有新增调用或历史摘要重建。详见[正式重启审计](E:/Agent/travel_agent/evals/summary-quality-v4-runtime-restart-2026-10-15.json)。

| 阶段 | 实施结果 |
| --- | --- |
| A：共享输出契约 | 已实现常量、本地schema、精确错误类别/路径；未扩充引用上限 |
| B：有限语义保护 | 已修复四个旧反例，增加对象/数值/单位、比较、未决、条件、执行状态和引用否定边界测试 |
| C：提示与版本 | 已接入用户md原文，版本v4，发布保存内容哈希；统一message_id输入 |
| D：降级与重建 | 已实现安全失败日志、可观测旧版回退、显式预算的一次结构修复；交付单会话重建策略，未执行正式重建 |
| E：同语料验证 | 严格离线评测和8次历史真实响应回放已完成；没有新v4真实模型生成结果 |
| F：横向迁移 | 已处理研究字符契约、定时查询截断、图片预约语义；其他入口保持原样，后续按模块迁移，不全项目重写 |

用户提示文件没有被本次实现改写。当前运行时读取内容为2692字符，SHA256：

```text
9616b673176e3cf12aee619f15519699b40cd350804eaeb68d35c208cdf07e78
```

此哈希针对Python读取后实际发送的文本，不是操作系统换行差异下的文件字节哈希。

## 2. 核心修改

### 2.1 提示与契约

`core/summary_contract.py`定义12项、1800正文字符、4来源、500引用字符及四种kind；本地schema与validator共用限制，提示加载时检查约定是否漂移。保留JSON object接口模式，不假设网关支持严格schema。摘要字数合计、来源连续性、所属/删除等仍由后端检查。

`services/conversation_summary_service.py`不再维护另一份硬编码system，直接读取[用户提示词](E:/Agent/travel_agent/docs/prompt_design/abstract_design_prompts.md)。新payload使用message_id，保留输入顺序；旧id只兼容历史捕获回放。previous_items去掉内部source_hash后传给模型。

未向模型提供saved_business_states，因此没有让模型自行猜测哪些旅行约束已经入库。现有敏感/遗忘过滤没有取消。

NapCat镜像Dockerfile补充复制这一个运行时必需的md，不打包整个文档目录；否则容器里只有services/core而缺少提示原文，会安全失败而无法生成摘要。本轮只做构建上下文静态检查，没有拉取镜像或部署容器。

### 2.2 校验

`core/summary_semantics.py`是有限反例防护，不是通用自然语言语义证明器。

已验证：

- 正确的“20:30改19:30”，即使引用含“此前不再适用”，也不再因全局否定布尔比较而误拒。
- 人民币/CNY、两个月/2个月等有限等价表达可以通过；未给币种时仍不能补CNY。
- “不是父母，是姨妈”反写、每间每晚/每人每天反转被拒绝。
- 同一条目不同数字不能借另一费用对象的单位通过；显式连续行走的多主体数值互换被拒绝。
- 最多→至少、未决→决定借用、不借用→不需要、条件删去、愿望→已执行等固定反例被拒绝。
- 仅连续子串也不必然支持事实：不能从“不安排夜游”裁出“安排夜游”引用来反转含义。

这些保护没有完成任意中文表达的实体消歧、跨句时态或逻辑推理；不能声称每条自然语言改写都被自动证明正确。

### 2.3 请求、版本与诊断

分批除原24000字符限制外，还扣除system/消息结构的保守字节估计。新增提示较长，旧分批在Q07会触发网关输入上限；这次离线回归发现并修复该问题，没有靠增加上限解决。

仓库新增可空历史兼容的prompt_hash字段；发布的prompt_version改为共享v4常量。format_version仍为3，旧摘要不会只因提示变化被批量删除。

失败日志与回调能区分模型/截断/解析/校验/发布问题，并给出如sources_count、items[10].sources、actual=5、max=4等信息。不默认记录正文。

ContextBuilder仍只按完整条目渲染，不切半条引用，并增加被注入摘要的提示版本/哈希诊断；只读检查工具可区分有效旧版与当前提示生成的版本。

### 2.4 结构修复与回退

默认不额外调用模型。只有调用方显式接入并预留物理调用/Token的repair_permit时，才允许一次结构修复；修复后全量重验。语义、出处或遗忘失败不走修复，不裁掉第5条来源冒充成功，也没有引入循环评审模型。

失败复用仍通过来源/依赖复核的旧摘要；没有有效旧版本时不注入派生摘要。冷却和发布租约仍有效。

## 3. 其他提示相关改动

- 研究汇总：明确300字符是目标、600字符是硬上限，最多8项；提示与validator读取同一契约，版本research-synthesis-v3。
- 定时查询：采用共享response_json，截断/内容过滤的合法JSON也不会创建任务，版本scheduled-query-v2；工具和原文权限未扩大。
- 预约图片：明确“无需提前”不等于“无需预约”。需要预约但没有提前规则时保留true＋none，草稿提示人工核对并阻止自动生成开约提醒。只有“无需提前”而无法确认预约状态时提示模型仅保留raw_text。版本reservation-image-v2。
- 没有迁移或重新识别旧图片，没有实现自动现场预约，没有修改普通提醒授权规则。

[全项目提示词清单](E:/Agent/travel_agent/docs/prompt_design/project-prompt-inventory-2026-10-14.md)已同步以上版本、当前作用和边界。P08链接到唯一提示原文，避免复制成另一份维护源。

## 4. 验证证据

- [严格离线摘要评测](E:/Agent/travel_agent/evals/summary-quality-v4-offline-2026-10-15.json)：Q01—Q09机制通过，known_guard_failures为空，真实模型调用0。固定理想响应只验证机制，不作为模型召回分数。
- [历史真实响应回放](E:/Agent/travel_agent/evals/summary-quality-v4-replay-2026-10-15.json)：离线重验8个已捕获响应，原5个可发布结果仍可通过；请求8正确时间条目通过；请求1/7/8的5引用问题仍拒绝。没有重新生成摘要。
- [生产装配离线回归](E:/Agent/travel_agent/evals/summary-quality-v4-production-offline-2026-10-15.json)：120/120通过。报告221次为脚本模拟调用；usage缺失不代表真实Token消耗，不能当成新增221次真实请求。
- 全量后端首次运行874项，有2项未通过，证据保留在[首次回归报告](E:/Agent/travel_agent/evals/summary-quality-v4-backend-2026-10-15.json)。旧摘要重建测试仍返回v2模拟顶层字段，已更新为当前wire协议，并另测旧输出拒绝；执行超时/租约测试单独17项通过，未修改其业务代码和断言，不能仅凭首次超时认定业务回归。
- [最终全量后端回归](E:/Agent/travel_agent/evals/summary-quality-v4-backend-final-2026-10-15.json)：Python3.11.15，875/875通过，435.797秒，测试期间后端源码未变化。与首次运行相比，没有放宽超时租约测试断言；最终串行全量通过。首次失败报告保留，不将一次通过当作已证明消除全部时序波动。
- [正式默认Web库临时副本迁移演练](E:/Agent/travel_agent/evals/summary-quality-v4-migration-2026-10-15.json)：只新增prompt_hash，原字段数据一致，SQLite完整性检查ok，重复初始化成功；未写正式库。

专项已通过摘要/预算、预约、图片、定时查询及研究模块；完整后端覆盖其他路由、会话隔离、遗忘、发布租约和现有用户改动。没有前端源修改，本次不重复覆盖用户当天的前端或服务配置变更。

## 5. 尚未执行与验收方法

本轮真实模型调用为0，费用没有新增。上轮12次物理请求额度已用完，本轮未重新使用或扩容。

离线通过不证明新版模型已经找回Q03人物/预算、Q02人数房型、Q06纪念册。未来取得新预算后，在相同模型/档位和语料下重新生成摘要，再按原金标准比较；不把金标准送给模型，不把旧响应回放说成新提示效果。

正式重启和备份已完成，操作记录及旧摘要重建约束见[部署与重建策略](E:/Agent/travel_agent/docs/summary-v4-deployment-and-rebuild-2026-10-15.md)。正式库仍仅有原来的stale摘要，未批量清空/重建任何会话；今后满足原触发条件时才会按v4生成新摘要。

离线复现命令：

```powershell
Set-Location E:\Agent\travel_agent
D:\anaconda3\envs\agent\python.exe -X utf8 scripts/evaluate_summary_quality.py --strict-guards --output evals/summary-quality-v4-offline-recheck.json
D:\anaconda3\envs\agent\python.exe -X utf8 scripts/replay_summary_responses.py --output evals/summary-quality-v4-replay-recheck.json
D:\anaconda3\envs\agent\python.exe -X utf8 -m unittest discover -s tests -p 'test_summary*.py' -q
```

以后进行用户层面真实验收时：在同一测试会话先给出人物、不同费用口径、共住/房型、名称、未决事项；再明确更正费用、时间和一项决定；确保触发摘要后，再用至少16条中性消息使关键原文退出两条历史通道。只读检查确认实际注入版本，然后分别测试摘要回顾、限制应用与未提供信息。未知日期/医院等必须明确未知。该流程会调用模型，不能在本轮已耗尽额度下执行。

升级没有改变模型、扩充摘要条目数/输出Token，也没有全局扩大历史窗口。
