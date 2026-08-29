# ASTRA 整改计划：对齐 榜首闭源方案 路线（仅 PI + Node + 极短通用提示词）

> 2026-08-29 定稿，**同日实施完成（阶段 A+B 合并执行）**，分支 `cairn-y`，版本 0.2.0，148 测试全绿。
> 依据：用户指令（三条）+ l3yx《众人向左，我偏向右》逐字解读（`docs/榜首闭源方案文章解读与架构提炼.md`）。
> 纪律：三阶段各自独立可发车、可回滚；一次只动一层；每阶段有验收门，不过门不进下一阶段。

## ✅ 实施进度（2026-08-29，分支 cairn-y）

| 项 | 状态 | 提交 |
|---|---|---|
| 提示词重建（495→218 行，删 4 个审查/压缩/侦察模板） | ✅ | 0e0920c |
| server FGS 数据模型（Step+expect/status、Finding、SubGoal、迁移） | ✅ | ebec3b7 |
| dispatcher 核心（decide/execute、砍审查链+consolidate、仅 pi|mock） | ✅ | 7c915b6 |
| 前端 FGS 化 | ✅ | 1699ee8 |
| 引擎 pi 化（舰队/watchdog/自检/usage） | ✅ | e551bbc |
| 镜像（pi@0.73.0，卸 claude-code+playwright-mcp） | ✅ | 409167f |
| 测试迁移 148 全绿 | ✅ | a4a958e |
| 版本 0.2.0 + note/release | ✅ | 本提交 |

**✅ A1 spike 已完成（2026-08-30，本机实测）**，三项实证发现已落码：
1. pi 0.73 的 anthropic 协议 API id = `anthropic-messages`（非 "anthropic"）——引擎默认值与示例已修（cb84161）
2. 环境变量 ANTHROPIC_AUTH_TOKEN 会覆盖 models.json 的 apiKey——引擎启动清洗污染 env（cb84161）
3. pi 会话文件落盘在 dispatcher 语境不稳定（手动直跑可写、Popen 复刻稳定为 0）——执行层已去会话依赖：execute 超时流式天枢抢救（stdout 双格式行解析）+ execute.md 流式条款 + phase usage 事件记账（c713402）；conclude 续接保留为 best-effort

**✅ 本地全链路性能测试三轮全过**（合成靶机 http://127.0.0.1:18765 + 真实模型，tmp/perf_local.py）：
- 三轮均 42-45s 完整解题（bootstrap 探测→玉衡定航→摇光执行→flag 写回→归航）；引擎冷启 1.6-4.6s；3 worker 启动健康检查全过（3.8-4.1s/个）
- 单轮 token 记账生效：bootstrap ~10,002 tokens/题（phase usage 日志）
- 复现性 3/3；测试后已清理含密钥的 spike/agent 目录

**待办**：①平台实弹轮需新 BENCHMARK_TOKEN（旧 token 任务已 finished，需平台开新一轮）②镜像重建+容器冒烟 ③打包 v8（用户指示暂缓）

---

## 〇、方针（用户指令复述）

1. **执行底座只留 PI**：dsh（已删）、claudecode、codex 全部弃用。理由：PI 最原始、完全可控——Less is More。
2. **技术栈 Python → Node**：CC/Codex/PI 都是 Node 生态，嵌入 PI 的 Agent Loop 必须进它的生态。
3. **内置提示词极短、与安全任务解耦**：引擎是通用任务解决引擎，安全知识只进任务描述层，不进引擎内置提示词。

由文章推论的架构对齐项（随阶段 B 落地）：
- 图语义 Fact/Intent/Hint → **FGS**（Fact=已确认事实 / Goal=终止条件可挂 SubGoal / Step=行动+预期产出事实）+ **Finding** 节点；append-only，无压缩。
- 活动收敛为 **Decide**（串行、事件触发、干净上下文、只有图工具）+ **Execute**（世界工具 + submit_fact 自证入图）。
- **砍审查环、砍 consolidate、砍 KB/playbook 提示词注入**（工具安装类的环境预备保留）。

## 一、现状盘点（实勘 2026-08-29）

| 项 | 现状 | 目标 |
|---|---|---|
| 执行底座 | claudecode 现役（DS explore×2 + GLM reason×2）；codex 注册残留（`adapters/__init__.py`、`config.py:16`） | 仅 pi |
| pi adapter | **已存在且成熟**（`adapters/pi.py`）：`--no-extensions --no-skills --no-prompt-templates --no-themes --no-context-files` 全关、models.json 注入任意 provider（baseUrl/api/apiKey）、`--mode json` 事件流、会话续接 `--session`、思考档位 `thinkingLevelMap`+`compat.thinkingFormat`、Windows node 直跑 | 直接启用，小补 |
| 引擎语言 | Python（dispatcher + runner） | Node（阶段 C） |
| 内置提示词 | 9 个 .md 共 **495 行**（reason 87 / verdict 67 / bootstrap 62 / explore 57 / challenge 52 / consolidate 46 / conclude×2 83 / platform-recon 41），外加图摘要/KB/playbook 多层注入 | 每角色 ≤30 行极短通用版，删注入层 |
| 图 | Fact/Intent/Hint + consolidate 压缩 + 双星审查（verdict/质询/裁决） | FGS + Finding，append-only，无审查无压缩 |
| 镜像 | Dockerfile.slim 装 claude-code@2.1.250；**未装 pi** | 装 `@mariozechner/pi-coding-agent`，卸 claude-code |
| 版本 | astra `0.1.0`（pyproject.toml） | 阶段合并点 bump |

**关键依赖（实勘发现）**：`pi.supports_review()=False`——审查目前回退到 claudecode worker 执行。所以**删 claudecode 之前必须先把审查默认关掉**（审查本来就在砍除清单里，顺序天然一致）。

## 二、三阶段路线

### 阶段 A：PI 接管执行层（Python 引擎不动）——预计 1-2 天

| # | 任务 | 落点 |
|---|---|---|
| A1 | **spike：pi 双网关实测**。DS anthropic 网关 + 智谱 anthropic 网关各跑：headless json 模式、会话续接、bash 工具长调用、中文长 prompt。重点复测历史观察"审查场景偶发提前退出（message_start 后 rc=0）"在执行场景是否复现；复现则记录形态定规避 | 本机，产出记录进本文档附录 |
| A2 | 引擎舰队渲染器 pi 版：拓扑沿用 R10（DS explore×2 maxrun3 + GLM reason×2），底座换 pi；env 组切换 `PI_BASE_URL/PI_API_KEY/PI_PROVIDER_API/PI_MODEL/PI_MODEL_REASONING/PI_THINKING_LEVEL(_MAP)/PI_MODEL_COMPAT` | `astra_runner_engine.py` 新增 `_render_pi_fleet()` |
| A3 | **先加 `ASTRA_REVIEW_ENABLED` 开关（默认 0）**，再删 `claudecode.py`/`codex.py` 适配器与注册表引用，`WorkerType = Literal["pi","mock"]`；审查/consolidate 相关测试改开关断言 | `adapters/`、`config.py`、tests |
| A4 | 镜像：npm 装 `@mariozechner/pi-coding-agent`（双镜像源+硬失败，照抄 CC 安装套路）；卸 claude-code；PATH/入口验证 | `Dockerfile.slim`、`Dockerfile` |
| A5 | 提示词第一轮纯删减：去 KB/playbook 注入段（重写留给 B5） | `prompting.py` + 调用处 |

**验收门 A**：本地 3 题冒烟（e1/e2/f1 各一）全链路（开题→explore→reason→交旗）；容器内 pi pong；同题 CC vs pi 的 token/耗时对比（**CC 税首次量化**）。

### 阶段 B：FGS 语义 + Decide/Execute 化（仍在 Python）——预计 3-5 天

| # | 任务 | 落点 |
|---|---|---|
| B1 | Intent → Goal（终止条件+动态 SubGoal）+ Step（行动+预期产出事实）拆分：协议、db、渲染 | dispatcher 协议层 + graph 存储 |
| B2 | Finding 节点类型：tsecbench 场景 Finding ≡ flag 事实（不参与计分）；SRC/审计场景=漏洞（通用平台产出层） | graph schema |
| B3 | Decide 化：reason 强制串行（maxrun=1）、事件触发保持、**干净上下文**（prompt 仅图视图，不带历史对话段）、不给 bash/靶机（只图操作） | scheduler + reason prompt |
| B4 | Execute 化：explore 写回语义 = submit_fact 自证（已确认才写）；审查/consolidate 代码在开关验证一轮后删除 | worker 协议 + 调度 |
| B5 | 提示词重写：9 个 495 行 → 每角色 ≤30 行极短通用版；安全内容只留 challenge.md 任务描述层 | `prompts/default/*` 重写 |
| B6 | 测试：FGS/Decide 新语义测试；删审查/consolidate 断言 | tests |

**验收门 B**：本地全量 42 题一轮不回退（对比 R10 基线）；**每题模型调用数与 token 显著下降（本阶段核心 KPI）**。

### 阶段 C：Node 引擎 ASTRA-Y——1-2 周量级，长线（赛程允许时）

| # | 任务 | 说明 |
|---|---|---|
| C1 | monorepo（npm workspaces）：`packages/agent`（PI 库嵌入的 Agent Loop，注入工具=图操作+世界工具+submit_fact）+ `packages/engine`（FGS 存储/Decide-Execute 编排）+ `packages/runner`（平台编排移植） | PI 从 CLI 子进程升级为库嵌入——文章的最终形态 |
| C2 | runner 适配件原样移植：生命周期/多旗收割/defer 预算/hint 购买/价值排序/watchdog 自愈/打包链 | **这是我们的赛制经济学层，文章没有这层——通用引擎+适配件分层，正是用户定的平台定位** |
| C3 | 对等指标：本地 42 题分数 ≥ R10 基线、token/题 ≤ 阶段 B 水平；Python 版冻结为 fallback 分支不再新增功能 | 双栈期只修 bug |
| C4 | 镜像换 Node 基础；版本 bump：astra `0.1.0` → `0.2.0`（B 合并点）/ `1.0.0`（C 合并点） | pyproject → 阶段 C 后 package.json |

## 三、明确保留（我们的增量，与文章不冲突）

- **赛制经济学层**：价值排序 / defer 预算 / hint 购买 / 多旗收割 / 平台回执入图——文章是通用引擎没这层；我们的定位=通用引擎（engine）+ 赛事适配件（runner）。
- **环境预备**：radare2/ghidra/qemu 等工具安装（容器层事实，非提示词注入）。
- **自愈体系**（四层 watchdog）与打包链：运维层与底座无关。

## 四、风险表

| 风险 | 应对 |
|---|---|
| pi 执行场景提前退出（历史观察在审查场景） | A1 spike 首项复测；执行场景无历史记录，实测定论 |
| pi 会话续接/上下文管理与 CC 行为差异 | A1 实测清单覆盖；必要时 explore 退化单轮无会话（文章本来就是短会话派） |
| GLM 走 pi 的 compat 形态（thinking+reasoning_effort） | models.json `compat.thinkingFormat=deepseek` 设计已备，A1 实测 |
| 删 claudecode 时审查回退断链 | A3 顺序强制：先 `ASTRA_REVIEW_ENABLED=0` 再删 |
| R10 托管轮在跑 | 阶段 A 只动 worker 层与镜像，不碰 R10 包（e18559d9 已交付）；复盘可与 A 并行 |
| Node 双栈维护成本 | C 阶段 Python 冻结，只修不加 |

## 五、执行顺序与 git 纪律

- 分支：`stage-a/pi-only` → 合并 → `stage-b/fgs` → 合并 → `stage-c/node-y`；细粒度 commit，严禁 co-author。
- 每阶段验收门不过不合并；env 变更全部可回滚（旧 env 文件保留一份 `_cc` 后缀备份）。

## 附录：A1 spike 记录（待填）

（pi 双网关实测结果写这里）
