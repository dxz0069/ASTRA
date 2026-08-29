# ASTRA 与上游 Cairn 对比清单（2026-08-28，四路交叉审计之一）

> 对比基准：`upstream/main` = oritera/Cairn@8f702c5（2026-07-16）vs 本仓库 HEAD。
> 方法：git 快照基线比对（两历史无共同祖先——fork 为源码快照非 git fork，
> merge-base 退出码 1；经 tree 比对确定事实基线 = 上游 2b86fba，2026-06-03）。

## 1. 分歧度总账

| 维度 | 数值 |
|---|---|
| 变更文件 | 288（含 47 处 cairn→astra 改名、189 新增、40 删除、12 原地大改） |
| 行数 | **+73455 / −17573** |
| 核心代码区（astra/src ↔ cairn/src） | 131 文件 +11843/−17180 |
| 新增独立子系统 | ≥6（见 §2） |
| 测试 | 13 → 19 个文件（+3744 行） |
| 提交历史 | 85（我们）vs 69（上游） |

**结论：工程实体上已是独立项目**——内核思想同源（黑板三原语），但编排层、记忆层、
技能库、执行栈、测试体系全部为自建或重写。

## 2. 我们有、上游没有的（六大子系统）

1. **container/astra_runner/ 编排层**（+2942 行）：runner（经济调度：开题序/defer 预算/多旗收割/hint 门控/时间窗）+ engine（舰队渲染/自愈）+ model_watchdog + supervisor 脚本。上游无任何编排层。
2. **技能库 container/.agents/skills/**：23 个技能目录 +41559 行（web/pwn/reverse/crypto/killchain/cloud/blind-sqli/spring-jolokia…），上游同位置仅 1 个 tsec-actions（已弃）。
3. **L4 知识库 container/knowledge/**：challenge-approaches/dead-ends/memory-stats，跨轮经验固化；上游无。
4. **tools/ 四件**：merge_knowledge（记忆固化）/run_status（态势板）/distill_review（赛后蒸馏）/check_pro_activation。上游无 tools/ 目录。
5. **自研 local 执行模式**：local_containers.py + local_process.py（宿主目录+subprocess 替代 Docker）。
6. **记忆-上下文管线**：context.py（写时治理/负结果保活）、embeddings.py、consolidate 任务、11 个新 prompt（含 challenge/verdict/consolidate）。
   另：server 前端重写、双星审查体系、服务端安全加固（auth/租约令牌/安全头/请求体上限）。

## 3. 上游有、我们没有的（fork 后上游的 3 个实质提交）

| 提交 | 内容 | 对我们的意义 |
|---|---|---|
| 233f5e8 local execution mode | ExecutionBackend/LocalBackend 协议抽象（runtime/backend.py + local_backend.py）：local 模式直接复用宿主已登录 CLI，免 Docker 免配 key | 我们有等价物（local_containers）但无统一 backend 抽象——**分差候选低**（功能已覆盖） |
| 668d339 code-based healthcheck | 进程内 HTTP 探活（workers/health.py：claudecode→/v1/messages 2xx 即健康），**删除 curl 一次性容器路径** | 我们仍用容器内 curl 探活（保留上游已删的 _curl.py）——**效率差异候选**：我们每次探活起 curl 子进程，上游纯 Python HTTP。分差贡献小但值得吸收 |
| 5a065ff | 示例配置注释 | 无功能影响 |

上游测试 test_healthcheck.py / test_local_execution.py（上游版）未移植。

## 4. 同名不同实现（重写度）

- **claudecode 适配器**：上游为薄封装；我们重写为会话目录稳定复用 + playwright MCP 注入 + SMALL_FAST/SUBAGENT 钉主模型 + CC 会话 jsonl 用量解析。
- **prompts**：上游 default 三件（bootstrap/explore/reason）已删，换自研全套（攻击面清单起手/反方四问/同族传导/负结果收束）。
- **dispatch 配置体系**：上游静态 yaml；我们 env 动态渲染舰队（engine 内生成）。

## 5. 待吸收清单（按价值排序）

1. 进程内健康检查（health.py 思路）——省掉容器内 curl 子进程，探活更快更稳；
2. ExecutionBackend 抽象——为接入更多执行环境（远程沙箱/云容器）铺路，与"通用平台"定位一致；
3. 上游健康检查/local 执行两套测试用例。

## 6. 分差来源判定（事实层，归因见差距分析文档）

上游新提交均为**运维便利性**改进，非能力跃迁——**分差主因不在上游这 3 个提交**，
而在榜首作者的实际运行形态与我们编排/执行行为的差异（见 docs/榜首差距分析.md）。
