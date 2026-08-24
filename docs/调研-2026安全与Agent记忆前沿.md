# 2026 安全顶会/前沿调研：与 ASTRA 相关的新进展

> 2026-08-24 调研。来源：arXiv 2026、USENIX Security '26 接收列表、OpenReview。
> 结论先行：**学界正在验证 ASTRA 的两个核心押注**——(1) 记忆是深度任务的决定性组件；(2) "为持久挑战而非模型局限设计，agent 价值随模型升级而复利"。

## 一、最重要的五篇（按相关度）

### 1. What Makes a Good LLM Agent for Real-world Penetration Testing?（arXiv 2602.17622）
分析 28 个系统的失败学，提出 Type A（能力缺口，工程可修）/ Type B（复杂度壁垒：实时难度评估、探索-利用平衡、上下文耗尽）二分法。PentestGPT v2 = 38 工具类型化接口 + 任务难度评估 TDA + 证据引导攻击树搜索 EGATS（MCTS+难度惩罚，TDI>0.8 剪枝）+ 外部状态存储记忆。

**关键数据**：消融（GPT-5.2，XBOW 104 题）：54% 基线 → 68%（工具层）→ 77%（TDA+EGATS）→ **85%（+记忆）**；上下文负载 >40% 后准确率从 90% 跌到 61%——印证我们零膨胀预算的正确性。深度任务 79% 失败是 Type B。

**对 ASTRA 的验证与启示**：
- "为持久任务挑战设计而非模型临时局限，agent 价值复利而非随模型升级蒸发"——**这就是我们的答辩主张，学界原文背书**；
- 记忆消融 +8pt，是深度任务最大单一增益组件；
- 我们已有：defer≈剪枝、期望预算≈TDA 雏形、星图≈State Store、Epitome≈branch summary；
- **可抄**：① 状态卡片——把主机/服务/凭据从自由文本星记结构化抽出、常驻内联（比我们的"关键钉住"更进一步）；② 40% 上下文阈值作为 context_budget 标定依据。

### 2. Infini Memory：Maintainable Topic Documents for Long-Term LLM Agents（arXiv 2606.10677）
记忆=按主题组织的文档（而非孤立条目/追加日志）：staging buffer 暂存 → 周期性合并进主题文档 → 支持**修订**（覆盖过期事实而非双存）；检索是**迭代式 agentic retrieval**（LLM 通过工具调用多跳浏览记忆，非一次性 top-k）。MemoryAgentBench 64.7%。

**对 ASTRA**：我们的 challenge-approaches 按题=主题文档 ✓、/tmp 沉淀=staging buffer ✓、merge 脚本=合并 ✓；**差距两点**：① 修订语义——条目过期应覆盖+留元数据（我们冲突检测是双版本保留，可加"战况变化时标注适用条件"）；② 把星图浏览做成 worker 可多跳读的接口（graph.yaml 文件引用已是雏形，可升级为"先看主题索引再展开"两层结构）。

### 3. CTFExplorer：Multi-Target Web CTF 评测（arXiv 2602.08023）
评测范式从孤立单靶转向**多目标共享设施**（更接近真实渗透）。**对 ASTRA**：Tsecbench/BSRC 正是多题多目标场景——我们的并行窗口+跨题记忆复用恰好是这个范式下的优势设计，答辩可引此说明评测设置的先进性。

### 4. CTFusion：Live CTF 流式评测（OpenReview）
用进行中的真实比赛流式出题，避免静态基准的数据污染。**对 ASTRA**：托管轮"未知新题"就是这个逻辑的实践——无污染、防背题，可作为我们成绩可信度的论据。

### 5. A Survey of LLM-Driven Penetration Testing（arXiv 2607.02605）
81 篇 Agent4Pentest 论文（2023-2026）六分类 + 四阶段架构演化。作为答辩"相关工作"引用源，把 ASTRA 放进演化叙事的"记忆/经验层"位置。

## 二、记忆架构研究趋势（与四层模型对照）

- **Multi-Layered Memory（arXiv 2603.29194）**：working/episodic/semantic 三层+自适应检索——与我们四层同构，说明分层是学界收敛方向；
- **Memory in the LLM Era（arXiv 2604.01707）**：统一框架把记忆方法拆四个模块化组件做系统比较；
- 标准基准：**LoCoMo / LongMemEval / BEAM**（Mem0 等在上面刷分，92-94 区间）——**空白点：没有安全场景的记忆基准**。这是个机会：我们的"记忆增益对照"（关/开记忆 A/B）如果做成可复现脚本，就是安全领域的第一个记忆基准雏形，够写一篇 workshop。

## 三、产业侧进展

- **Google Big Sleep**：真实零日（SQLite CVE-2025-6965）证明 agent 挖洞可行，后续 OpenAnt、Revelio（成本高效的 agentic 漏洞挖掘）等一整条线在跟进；
- 2026 渗透 Agent 工具已 39+ 款（appsecsanta 盘点），CHECKMATE（LLM+经典规划）等学术系统涌现——**赛道在快速拥挤，但全部集中在"单次任务能力"，记忆/经验复利方向依然只有我们在做**。

## 四、行动清单（按优先级）

1. **状态卡片**（抄 PentestGPT v2）：星记里结构化抽 主机/端口/凭据/会话 四类，常驻内联——与"关键钉住"互补，约 1 天；
2. **40% 上下文阈值**：用论文数据标定 context_budget 的默认值并写进文档（学术依据）；
3. **答辩引用包**：PentestGPT v2 的记忆消融数据（+8pt）+ "价值复利"原文 + CTFExplorer 多目标范式——三处学界背书直接进答辩 Q&A；
4. **记忆修订语义**：merge_knowledge 加"同条目战况变化→标注适用条件"（Infini Memory 的 revision 思想）；
5. **（赛后）安全记忆基准**：把 A/B 对照脚本标准化，瞄准"安全场景第一个记忆基准"的空位。

## 附：来源

- PentestGPT v2: arxiv.org/html/2602.17622v1 ｜ Infini Memory: arxiv.org/abs/2606.10677
- CTFExplorer: arxiv.org/html/2602.08023v3 ｜ CTFusion: openreview.net/forum?id=2zQJHLbyqM
- Survey: arxiv.org/html/2607.02605v1 ｜ Multi-Layer: arxiv.org/html/2603.29194v1
- Big Sleep: projectzero.google/2024/10/from-naptime-to-big-sleep.html
- USENIX Security '26 Cycle 1: usenix.org/conference/usenixsecurity26/cycle1-accepted-papers
