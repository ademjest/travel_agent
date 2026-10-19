# 旅行攻略地域选源与项目 skill

日期：2026-09-21

真实接口验收补全：2026-09-22。

## 现状核对

旧搜索只向 Tavily 发送原始查询，使用 basic 搜索、最多 5 条结果，没有站点域名限制或显式检索语言偏好。只读查看旧研究记录时，确实发现“武汉三日游攻略”曾返回 Marriott、Reddit、YouTube 等来源。不能把这个问题全部归因于小红书、知乎反爬：原实现没有先定向检索这些平台。

网页读取使用普通 HTTPS 请求及 HTML 文本解析，不执行 JavaScript、不使用登录 Cookie。因此索引覆盖、登录、动态渲染、访问拒绝和限流都可能影响获取正文。是否有开放平台不等于是否提供可读取任意攻略的 API；本项目当前使用 Tavily 公开搜索，没有接入知乎或小红书的专有内容 API。

## 本次实现

Skill 位于 `skills/travel-research/SKILL.md`，站点配置位于同目录 `sources.json`。这是 **travel_agent 项目的运行时策略**，不是只安装给 Codex 的个人 skill。

运行流程：

```text
研究请求 → 读取 skill 与站点配置 → LLM 判断目的地及用户平台偏好
→ 校验有限搜索计划 → 向 Tavily 发送实际域名过滤和语言偏好
→ 按来源组交错选取结果 → 读取公开内容 → 标注证据状态并总结
```

| 请求 | 默认行为 |
| --- | --- |
| 武汉三日游攻略 | 小红书／知乎 → 中文旅游平台 → 官方信息检索 |
| 中文询问京都、巴黎攻略 | 国际旅游平台 → 目的地官方信息检索；报告仍用中文 |
| 跨境／混合目的地 | 中文和国际旅游来源结合 |
| “只看小红书上的京都攻略” | 优先用户指定平台，严格限制时不作跨平台补充 |
| “不要小红书” | 搜索请求及结果校验同时排除该域名 |
| 模型或地域分类不可用 | 中立检索并说明；平台限制无法可靠解释时不擅自进行通用搜索 |

来源分组目前包含小红书、知乎、马蜂窝、携程、去哪儿、穷游，以及 Tripadvisor、Lonely Planet、Wikivoyage、Rough Guides。站点列表可维护；“官方信息检索”仍需核实返回网站的身份，不能仅凭搜索词含“官方”就认证来源。

最多三次计划检索，必要时补一次通用检索。查询完成后逐次存断点，读取失败可使用明确标注的搜索摘要。初次最多选择六条来源，正文均不可读时最多再加入两条补充来源。新查询读取更新后的 skill，已开始的请求保持已有计划。

新增对常见验证／登录标题和空动态页面的识别，避免把这些内容直接标为已读攻略。复杂网站仍可能存在文本不完整的情况，不能保证小红书和知乎的每篇内容都能获取。

## 代码入口

- `agents/research_search_planner.py`：加载 skill、校验模型搜索计划。
- `services/research_service.py`：执行域名优先级、补充搜索、断点与来源标注。
- `infrastructure/search_client.py`：Tavily `include_domains / include_domains_mode=restrict / exclude_domains / language` 参数，并在服务端再次验证返回域名。
- `infrastructure/web_reader.py`：公开页面文本和常见访问阻碍识别。
- NapCat Docker 构建增加复制 `skills/`，确保该入口也能读取策略。

参数依据：[Tavily Search API 文档](https://docs.tavily.com/documentation/api-reference/endpoint/search)。`restrict` 用于严格限定结果域名；`prefer` 允许站外结果。language 用于提高指定语言结果的排名，不等于强制排除其他语言。本次实测发现省略 mode 时仍可能混入站外结果，因此启用明确的 restrict，并保留后端域名校验。

## 验证与限制

- 全量离线回归 664 项通过；随后补充导航空页过滤，研究相关 31 项复验通过。
- Skill 格式检查通过，验证了国内／国外路由、显式平台覆盖、排除、域名伪装拒绝、登录／空动态页、有限补充搜索和中断恢复。
- 真实接口脚本 `scripts/smoke_research_sources.py` 已执行，使用临时数据库、真实模型与 Tavily 验证武汉和京都，不发送 QQ 消息。武汉用了 3 次搜索，返回知乎、去哪儿及武汉文旅等来源；京都用了 2 次搜索，返回 Tripadvisor、Lonely Planet、京都旅游网站等来源。部分页面只能取得搜索摘要，明确保留该状态。
- 另做小红书单站验证，确认只发起一次定向搜索，不跨平台补充。接口能找到笔记链接，但该例返回 HTTP 200 的“你访问的页面不见了”页面。这不能证明小红书全部不可访问，也不能据此确定是删除、访问限制还是反爬。
- 实测暴露了软 404 与导航／公司页脚误作正文的问题，已修复：错误标题不算有效正文，只有导航与页脚的搜索摘要也不作为旅行证据。该过滤使用捕获到的内容形态作离线回归验证。
- 脚本可通过 `python -X utf8 scripts/smoke_research_sources.py` 手动运行，最多八次基础搜索，需要已配置模型和 Tavily Key。
- 小红书专项可运行 `python -X utf8 scripts/smoke_research_sources.py --case xiaohongshu`，只检索该平台。本次真实验证共使用 8 次基础搜索，未修改正式业务数据。
- 不绕过验证码、登录、权限或付费限制。无法取得全文时明确保留摘要／失败状态；预算和时效敏感事实仍需合适的官方证据。

本次代码已保存在本地，未提交 GitHub；后续自行启动 `run_web.py` 后生效。

验收日志：`E:/Agent/research-sources-live.log`、`E:/Agent/research-xiaohongshu-final-live.log`、`E:/Agent/research-policy-final-full.log`。小红书最后一次现场记录是在导航摘要过滤补充前，已经从“已读正文”降为“仅摘要”；后续专项测试验证相同类型导航摘要会进一步标为不可用。
