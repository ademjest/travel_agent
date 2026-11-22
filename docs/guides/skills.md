# 应用 Skill 统一管理

本机制管理旅行应用自己的提示文本与资源。模型调用、工具授权、预算、校验、确认、幂等和事务仍由现有代码执行。

## 查看与维护

在项目目录运行 `python scripts/audit_prompt_assets.py --status`，查看 20 个阶段的 owner、版本、资源文件哈希和组合哈希；添加 `--output 文件路径` 可保存完整递归盘点。

登记入口是 `skills/manifest.json`，读取与快照实现是 `infrastructure/skill_manager.py`。业务代码使用明确的 stage ID，不按用户关键词猜测 Skill。正文使用业务包内的文本片段 JSON；片段是普通字符串，不是可执行模板语言。含时间、上下文、工具结果的拼接和 JSON 序列化仍在原 Python 装配函数中。

| 资产组 | 阶段 |
|---|---|
| intent | intent.compile |
| agent | agent.respond |
| travel-research | research.search_plan、research.targets、research.facts、research.synthesis、research.write |
| trip | trip.requirements、trip.plan、trip.local_day、trip.compare、trip.answer |
| reminder | reminder.personal、reminder.booking、reminder.scheduled_query |
| materials | materials.summary、materials.image |
| reservation | reservation.image |
| memory | memory.summary |
| hotel | hotel.quote_extract |

`travel-research/SKILL.md` 保留为两个研究阶段的共享指令；`sources.json` 只通过显式 resource 读取。摘要仍以 `prompts/conversation_summary.md` 为唯一正文，保留原文及原有摘要哈希校验。embedding、连通性探测单列为技术调用。

## 加载与恢复

启动时校验全部登记资产。新业务请求取得只读快照，同一请求的阶段及修复复用快照；同一文件在快照中只读取一次。下一次新研究请求仍会读取更新后的来源配置。资源读取返回独立 JSON 值，调用方修改它不会改变快照。

没有在线编辑器或热发布接口。清单变更需要重启。逐文件读取不能保证多文件发布的原子性；部署时应暂停接单、整体替换经验证的发布文件并重启，不能在有请求运行时逐个编辑生产资产。

首次业务模型调用将完整快照的版本与组合哈希绑定到既有事件检查点。重试可使用相同的保留文件；如果文件或受控合同变更、旧版本已经不可用，则在模型提交和额度预留之前明确拒绝续传。首轮不保存完整旧资产包，因此无法自动恢复已被覆盖的旧版本；应恢复原发布文件，或结束旧任务后让用户发起新请求。旧的未绑定版本任务也不会据此自动重发模型调用。研究写作原有的未知结果占位和禁止重复提交规则继续有效。无业务事件的独立调用只记录模型账本，不新建业务检查点。

## 可追溯与验收

`model_calls` 和诊断白名单同时支持 `stage_id`、`asset_version`、`asset_bundle_hash`、`system_hash`。原 `call_id`、purpose、prompt_version、模型、usage 和耗时继续保留。

资产组合哈希覆盖登记文本、资源、声明版本及明确列出的合同源码；不保存原始提示或用户输入。`system_hash` 是实际 system 消息内容列表的哈希，包含动态装配；它不是完整模型请求指纹，也不能用来跨身份复用业务结果。发布门禁另外保存源码、资产登记及各阶段哈希。

维护后执行 `python scripts/run_release_checks.py --output 新目录`。首轮迁移应保留完整消息、工具、输出协议和修复行为的离线等价证据。提示语义后续变化需要单独进行质量评测；外置资产本身不会减少模型 token，也不能证明准确率提高。
