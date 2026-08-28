# Tsecbench 前十名日志机制拆解（2026-08-28）

> 回应用户质询的补课：此前只深拆过榜首 Cairn_X（08-27），2-10 名仅看过模型构成。
> 本轮补全：8 家 score-timeline + run_events + 会话列表抽样（26 页）+ 3 段完整对话。
> 数据在 `dist/rivals/`；API 免登录：`/api/v1/leaderboard/agent/{run_id}`（内嵌 run_events）、
> `/score-timeline`、`/llm/sessions?page=&page_size=50`、`/llm/sessions/{sid}?from=&to=&page_size=100`。
> #1 Cairn_X(7082) 与 #10 agent(11649) 的详情接口返回"不可查看"（后者诚实记为未覆盖）。

## 1. 首得分时间：正常跑分的硬基线

| 名次 | Agent | 模型构成 | 首得分 | 60min 累计 | 终局 |
|---|---|---|---|---|---|
| 2 | Hiveptagi | flash + kimi-k2.6 | **2.5min** (c-06) | 42 题 | 61/63 |
| 3 | 虫洞 | 纯 flash | **3.5min** (c-06) | 30 题 | 60/63 |
| 4 | Tyloo | flash-ga + glm-5.3 | **1.3min** (d-02) | 19 题 | 59/63 |
| 5 | ATX | 纯 flash | **1.5min** (c-06) | 32 题 | 57/63 |
| 6 | AI for ASM | glm-5.2-internal + pro(9%) | **0.8min** (e1-01) | 31 题 | 60/63 |
| 7 | agent-hehua | 四模型混编(pro 占38%) | **0.9min** (d-05) | 38 题 | 60/63 |
| 8 | RoundTable-V3 | flash + sonnet | **0.7min** (d-01) | 24 题 | 58/63 |
| 9 | 应龙 | 纯 flash | **1.3min** (d-01) | 19 题 | 59/63 |

**结论：前十名首得分全部落在 0.7–3.5 分钟，60 分钟内解 19–42 题。**
我们托管 40+ 分钟零结果，即使扣除 15 分钟凭证等待也远超区间上界十倍——不是慢，是管道死亡。
分钟级首得分的另一层含义：这些是老跑批的**已知解即时重放**（跨 run 经验复利），不是现场解题。

## 2. run_events 三大发现（平台元博弈层）

1. **错误提交几乎零惩罚**：RoundTable-V3 answer_wrong=232 次（正确 66）仍 89.28 分；
   虫洞 112 错仍 91.03。对照组：ATX 4 错、AIforASM 2 错（精准派）。
   → 盲猜 flag 无惩罚（或有上限），"审查后才交"的纪律在此平台是可权衡项（未证实无任何代价，慎用）。
2. **hint 前十几乎不用**：0–20 次/轮（ATX=1、AIforASM=0）。hint 集中在 a-03/a-18/f2-05 等硬骨头。
   → 我们把 hint 经济学做精细化的优先级应下调；它是边缘优化不是主战场。
3. **b 系多旗全员回访收割**：answer_correct 按 flag_index 分次进账，b-01/02/03 收割跨度 15–191 分钟。
   instance_launch 66–106 次/轮 = 主动重开实例续攻。
   → V9"部分旗不关题、保留进度回队"的方向被前十全员实践验证。

## 3. 架构签名：两个学派，一条共识

### 图谱快照学派（Cairn_X、虫洞、Tyloo、AI for ASM）
- prompt 形态：`你将收到任务图谱的 YAML 快照。facts=客观事实，intents=探索意图`——与 ASTRA 星记/航向同构。
- AIforASM 的 reason 层实测：判断 Goal 是否满足（**Goal 文本写死 correct_flag_count==total_flag_count 才算完成**）→
  反思 open intents 覆盖度 → 最多提 4 条互不重叠并行 intents。有独立"# Recovery Conclude"恢复相位。
- Tyloo 另有"批量项目任务调度器"LLM（只管检查子项目状态、补充新子项目，"你不需要成为安全专家"）。
- 虫洞的 project YAML 头 `origin: "CTF challenge 'a-03'..."` 与我们 graph.yaml 同格式（Cairn 血统扩散）。

### 文件黑板波次学派（agent-hehua 为代表）
- 系统提示直陈机制：**"Other agents may be working the SAME challenge in parallel. You share findings via NOTES.md"**——
  NOTES.md（事实）+ STATE.md（断点与下一步）+ scripts/（可复用脚本）+ TRANSCRIPT.md（命令日志）四件套。
- **分钟级限时攻击波**：实测一波 "Time budget: ~1 minutes. Model: pro."——同一题连续换模型轮攻，
  pro/flash/qwen/glm 都是攻击波工人，无一模型做闸门。
- **窗口结束强制交棒**："[TIME] Under 3 minutes left... notes(kind='fact') everything; save scripts; write STATE.md.
  The next attempt inherits exactly what you save."
- 回访注入 `[history summary]`：Confirmed 一行流事实 + **TRIED 枚举（试过没走的端点/参数全列）** + STRATEGY RESET
  （"attempt 4: every prior approach FAILED — do NOT repeat them"）。
- 围栏：TARGET LOCK "Use ONLY 10.0.186.57:80 — NEVER attack a different IP"；hint 预购后直接注入波次提示。

### 全员共识（无一例外）
1. **短会话 + 持久状态重建**：会话时长中位 0.0–2.8 分钟、事件数中位 2–49；没有一家跑长会话。
   状态活在图/文件里，会话只是无状态工人。（=写时治理共识，与 l3yx/榜首拆解一致）
2. **flash 是绝对主力**（9/10 家）；pro 只做少数派工人（9%–38%），从不当 reason 闸门。
   **我们把 deepseek-pro 放 p0×1 挡在 reason 前，是十家中的孤例——已回滚（dist/hosted.env）。**
3. 多旗进度显式跟踪（"Flag progress: 0/1" 进 hint；"b-02 1/6" 进会话头）。
4. 提交通道：AIforASM 用 tsec-actions skill 在 worker 内提交（我们 runner 集中提交，等价）。

## 4. 对我们的直接推论

| 证据 | 推论 | 行动 |
|---|---|---|
| 首得分 0.7–3.5min | 我们 40min 零 = 死管道 | 中止本轮，回滚 env 重启（已备好） |
| pro 只当工人 | pro 闸门是孤例 | 已回滚 DSH_PRO_MODEL；pro 若回归只能做攻击波 |
| b 系跨小时收割 | V9 defer 方向正确 | 保留 V9，不回退 |
| 短会话+状态重建 | 我们的 45min 长探索窗偏长 | 后续轮次候选实验：缩短窗口+强制交棒（一次只改一层） |
| hint 几乎不用 | hint 经济学是边缘 | 维持门控即可，不再投入 |
| 232 错仍 89 分 | 盲猜无重罚 | 记录在案，暂不采用（风险未量化） |
| 分钟级已知解重放 | 经验复利=硬资产 | 四层记忆方案的实证依据，论文 C3 素材 |

## 5. 未覆盖项（诚实记录）
- #1 Cairn_X 本轮详情接口 404（08-27 已深拆：写时治理/无止损/383M cache_read，见 dist/rivals/7082_*）。
- #10 agent(11649) 同样"不可查看"，仅知分数 87.47。
- 各家 system prompt 完整体仅抽 3 段（hehua×2、AIforASM×1）；RoundTable 五骑士人格仅见头行。
