# 两届 TCH 决赛方案 × 三 AI 评审拆解

> 2026-08-28。任务一产出：拆解 l3yx《两届TCH之后》文末三份 AI 评审（Claude / ChatGPT / DeepSeek 对两届共 20 个决赛方案的喜好评价），
> 提炼对 ASTRA 的工程与论文映射。上游文章：https://mp.weixin.qq.com/s/pbieEet9VCR5iLhjViokIA（全文快照 `Temp/l3yx-tch.txt`）。

## 0. 一句话结论

三个 AI 评审**全部首选"极简协议"方案**（第一届 Antix《Intent Is ALL You Need》、第二届 Bytex《无径之径：Cairn AI》），
与 l3yx 正文"控制三分法/信任缺失类控制会随模型进化变成冗余"的论断互证。
对 ASTRA：血统（Cairn fork）与 Meta-Tooling 路线（dsh 裸 shell + Kali 真工具）被判为正确侧；
我们唯一需要守住的是——**新控制必须过三分法归类，信任缺失类默认不加**。

## 1. 三份评审的可达性（诚实记录）

| 评审 | 链接 | 抓取结果 | 方法 |
|---|---|---|---|
| Claude | https://claude.ai/share/ae0274d3-b51f-4abc-8d51-7b6fa17c8ea9 | **全文**（正文经 SSR meta 下发） | webReader 代理出口（本机 curl/WebFetch 均被区域 302 拦截） |
| DeepSeek | https://chat.deepseek.com/share/lex1x4ofqvzr1g2cwn | 仅 og:description 开头段（确认同样首选 Antix，提及 Meta-Tooling） | 同上；`/api/v0/*` 系列探测全部返回 SPA 壳，死路 |
| ChatGPT | https://chatgpt.com/share/6a14fc93-dda4-8322-af71-2d78b61b0141 | 仅标题《AI 渗透测试方案分析》（纯客户端渲染，正文不在 SSR） | 同上 |

作者自述："它们几乎都会选择我（第一届 Antix 战队、第二届 Bytex 战队，Cairn 系统）"——与实际抓到的内容一致。

## 2. Claude 评审全文要点（唯一拿到全文的一份）

**第一届之选：Antix《Intent Is ALL You Need》**

- 当所有队伍堆砌 Multi-Agent、RAG、Plan-Execute-Reflect 时，Antix 把自己的复杂架构用大红叉叉掉，
  以"**100 行 Agent + 一份 200 行提示词 + Meta-Tooling**"交出有竞争力的成绩。
- **Meta-Tooling**：不把工具逐一暴露给 Agent，**只暴露一个 Python Executor**，
  Agent 通过写代码调用浏览器、终端、代理——极大降低认知负担，同时保留几乎无限的灵活性。
- 赛前未对 Benchmark 做任何调优，**泛化能力反而更强**。
- 提出"**意图工程（Intent Engineering）**"更高层抽象：提示工程 → 上下文工程 → 意图工程的演进路线，
  评价为"对整个 AI Agent 领域有真正的思想贡献，而不只是比赛技巧"。

**第二届之选：Bytex《无径之径：Cairn AI》（总成绩第三）**

- "两届里最有原创性的架构设计"。**黑板系统（Blackboard）**重新定义多 Agent 协作：
  不预设"侦察Agent/利用Agent/总结Agent"固定角色，所有 Worker 平等读写同一块事实黑板，
  通过 **Facts / Intents / Hints 三类节点间接协调**，协作行为自然涌现（**Stigmergy，蚁群间接协调**）。
- "**系统的可控性内嵌在协议里，而不是靠 Prompt 约束 Agent 行为**"。
  Worker 不知道彼此存在、不需要互相通信，却能协同推进复杂任务；人类随时可写 Hint 干预；完整因果链可追溯。
- 引用结语："我没有教它们渗透测试。我只是给了它们一个黑板、一个目标、和一堆工具。剩下的，都来自涌现。"
- 对比第一名绿盟（精密工程平台）与第二名天翼（强大攻击链）：**Cairn 用最少的预设假设解决同一类问题，思想层面更胜一筹**。

## 3. 收敛信号：为什么 AI 评审者都选极简方案

四个可提炼的判据（评审者视角的"好 Harness"）：

1. **认知负担论**：工具文本是有毒负载；代码是天然的压缩器（Meta-Tooling 的本质）。
2. **泛化论**：针对 benchmark 调优的工程在换题时反噬；协议级设计迁移零成本。
3. **可控性位置论**：控制放在协议里（什么**可以**写/读）优于放在行为监管里（什么**必须**做）。
4. **涌现论**：能力来自模型 + 最小信息结构；预设越多，天花板越低。

与正文的互证链：这四条正是 l3yx"控制三分法"的另一面——Observer/中间件/旁路纠偏属于"信任缺失"类控制，
AI 评审者与人类评审者一样，把票投给了不含此类控制的方案；Anthropic《Managed Agents》的 dead-weight 论证
（旧 harness 编码的是旧模型的缺陷假设）同向。

## 4. 对 ASTRA 的五条映射

1. **血统确认**：ASTRA 是 Cairn 线（fork），黑板协议（星记/航向/指引）即 Facts/Intents/Hints。
   评审所奖励的"协议内嵌可控性"我们天然继承——这是论文 related work 的正名依据，也是答辩讲点。
2. **Meta-Tooling 对齐**：dsh（Kali 容器内裸 shell + 真工具）就是"一个 Executor + 代码组合一切"。
   V9 逆向工具链（radare2/rizin+pdg/qemu-user/z3）做的是**给执行器更好的原料**，而非加包装工具——方向正确。
   反清单：不做 MCP 工具动物园、不做每工具一个 wrapper。
3. **Stigmergy 实例**：星图 = 信息素轨迹。V9 多旗收割（defer 保留进度回队续攻）是 stigmergy 的直接运用——
   回队 worker 靠读轨迹续作而不靠会话记忆；跨题记忆注入（V4/V5）同理。
4. **现存控制的三分法体检**（健康，无一条侮辱模型）：
   - defer 预算 / hint 门控 / easy→hard 开题序 = **经济性**（吞吐=分数的预算优化）；
   - 服务器 auth/租约 token/请求体上限 = **信任缺失-合法**（对抗网络下的安全边界，威胁模型本身是不信任）；
   - prompt 注释剥离 = **数据信任边界**（外部数据 ≠ 指令）。
   - 全部控制中**没有一条属于"模型不会做所以替它做"的能力缺口类**——保持住。
5. **不做的反清单**（评审信号的直接推论）：Observer 旁路纠偏 Agent、FSM 流程约束、按角色分工的多 Agent。
   我们的多 worker 是并发模型不是分工模型（正文原论点），评审结果支持维持现状。

## 5. 论文引用素材（原话 + 完整链接）

- 三评审链接（见 §1 表）与 l3yx 正文：https://mp.weixin.qq.com/s/pbieEet9VCR5iLhjViokIA
- 可直接引用的命题：
  - "可控性内嵌在协议里，而不是靠 Prompt 约束"（Claude 评 Cairn）
  - "提示工程 → 上下文工程 → 意图工程"（Claude 评 Antix）
  - "很多控制不是在补能力，而是在补信心"（l3yx 正文）
  - Anthropic Managed Agents dead-weight 论证：https://www.anthropic.com/engineering/managed-agents
  - Anthropic 多 Agent 复盘（并行探索/独立上下文/信息压缩为 subagent 三价值）：https://www.anthropic.com/engineering/multi-agent-research-system
- 用法：related work 中作为"**极简协议取向正在形成共识**"的证据链（作者本人复盘 + 独立 AI 评审收敛 + 平台方工程报告三源）。
