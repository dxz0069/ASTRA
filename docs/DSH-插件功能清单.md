# DSH（DeepSeek Harness）插件/功能清单 —— ASTRA 视角

> 状态：2026 六小时靶场准备阶段整理。分「已落地 / 自带已确认 / 建议增强 / 明确不需要」。
> 依据：DSH 0.1.0-rc.6 源码与 README 实测 + 本仓库 `container/dsh/` 现状。

## 一、已落地（本次 claudecode→dsh 迁移交付）

| 组件 | 功能 | 位置 |
|---|---|---|
| `astra-headless-runner.js` | headless 会话续接：`--session <id>` → resume / 同 id create（create-or-resume）、`@file` 展开、`--help` | container/dsh/ |
| `astra-headless.patch.yml` | `--patch` 覆盖层：替换官方 runner + env 驱动的模型路由（deepseek-official / anthropic） | container/dsh/ |
| `e2e_verify.py` | 验收脚本：基础调用 / execute / 会话续接 / 落盘（真实模型 4/4 PASS） | container/dsh/ |
| DshDriver | 命令构造、healthcheck（deepseek/anthropic 双协议）、Windows node 直跑 | astra/…/adapters/dsh.py |
| 运行参数 | `DSH_PERMISSION_MODE=danger-full-access`（权限）、`DSH_HOME`（隔离）、`DSH_PROVIDER`（路由） | dispatch.yaml / engine |

## 二、DSH 自带能力（已确认可用，零配置）

| 能力 | DSH 组件 | ASTRA 价值 |
|---|---|---|
| shell / 文件 / 搜索 | `tool-bash` / `tool-fs` / `tool-fs-search`（ripgrep） | 侦察工具链（nuclei/ffuf 等在 bash 里调） |
| 工作区指令 | `agent-instructions` 加载 AGENTS.md/CLAUDE.md | 容器 `/home/kali/workspace/AGENTS.md` 已就位 |
| **技能体系** | `skill-filesystem` 默认扫描 `<project>/.agents/skills`（rank 200） | **已实测兼容**：`container/.agents/skills/astra-benchmark/SKILL.md` 的 frontmatter（kebab-case name + description）符合 DSH 格式 → DSH worker 自动获得靶场协作技能，与 claude 的 `.claude/skills` 等效 |
| 上下文压缩 | `compaction-basic`（token 超阈值自动摘要） | 长任务（单题 45min）防上下文爆 |
| 子代理 / 工作流 | `subagent`(spawn/fork) / `workflow` | 并行侦察、多角度分析 |
| 会话持久化 | `session-persistence-jsonl`（$DSH_HOME/sessions） | execute→conclude 双阶段 |
| 模型路由 | `deepseek-official`（chat-completions）+ `llm-pi-ai`（anthropic Messages） | DeepSeek / Kimi / 任意 claude 兼容端点 |
| 任务自律 | `tool-todo` / `tool-goal` / `repeat-tool-reminder` | 探索纪律（配合无效尝试抑制） |
| 结果裁剪 | `tool-result-pruner` / `spill-policy` | 大扫描输出不撑爆上下文 |

## 三、建议增强（按优先级）

### 🔴 P0 — 六小时靶场前（✅ 已全部落地）

**1. 时间上下文 ✅** —— `dsh-time-context` 已加入 patch，模型每步感知当前时间与已耗时（配合超时自律）：

```yaml
# astra-headless.patch.yml 内（已落地）
- id: time-context
  name: '@deepseek-ai/dsh-time-context'
  config:
    timeZone: Asia/Shanghai
    refreshIntervalMs: 300000
```

**2. 技能自动发现 ✅** —— 已实测兼容：`astra-benchmark/SKILL.md` frontmatter 符合 DSH 格式，DSH worker 自动加载 `.agents/skills`。

**3. anthropic 模式禁用 web_search ✅** —— `tool-web` 在 `DSH_PROVIDER=anthropic` 时 `disabled`（web-search-deepseek 无 key 会 MISSING_CREDENTIAL，且靶场内网用不到）。

### 🟠 P1 — 增强 Web 类题目的工具面

**3. Playwright MCP（浏览器工具）✅ 已落地** —— Dockerfile 已装 playwright（`PLAYWRIGHT_MCP_BROWSER=chromium`）且新增 `@playwright/mcp` 预装；patch 已挂 mcp-client（stdio，`failOnStartupError: false` 兜底）。e1 系列 Web 门户题可用真实浏览器（Cookie/JS 渲染/登录流）：

```yaml
# astra-headless.patch.yml 内（已落地）
- id: mcp-playwright
  name: '@deepseek-ai/dsh-mcp-client'
  config:
    serverName: playwright
    transport: stdio
    command: npx
    args: ['-y', '@playwright/mcp@latest']
    env:
      PLAYWRIGHT_MCP_BROWSER: !!js process.env.PLAYWRIGHT_MCP_BROWSER ?? 'chromium'
    failOnStartupError: false
```

> 实测：新 patch（time-context + MCP + tool-web 禁用）boot 与真实模型调用正常，MCP 拉起失败不阻塞任务。

**4. token 计量进靶场报告（评审量化"成本"）✅ 已落地** ——
- DSH 侧：`astra-headless-runner.js` 每次运行把本轮 usage（`assistant/chunk {type:'usage'}` 累加，含 input/output/cacheRead/cacheWrite/reasoning）追加写入 `$DSH_HOME/usage/astra-usage.jsonl`（session 维度，实测 `{inputTokens:0, outputTokens:22, cacheReadTokens:13487}`——billed input 三字段互斥符合 DSH TokenUsage 约定）
- runner 侧：`collect_dsh_usage()` 汇总，报告 JSON 新增 `total_tokens`（input/output/cache_read/cache_write/reasoning）——「效能美学」量化口径就绪

### 🟡 P2 — 可自定义扩展

**5. 题型模式库沉淀为 DSH skill** —— container/AGENTS.md 的「题型模式库」（e1 Set-Cookie flag / e2 Unpickler/vm2 逃逸 / e3 检测规避）已对 claude 生效；DSH 走 `.agents/skills`，可把每类题型拆成独立 skill（`web-portal/SKILL.md`、`deserialization/SKILL.md`…），按题动态注入。

**6. 自定义 DSH profile** —— 若未来需要多模式（如"快速侦察模式"与"深度利用模式"），用 `dsh plugin --profile <name>` 建独立 profile，而非继续堆 headless 补丁。

## 四、明确不需要（及理由）

| 组件 | 理由 |
|---|---|
| `dsh-tool-bash-persistent` / `dsh-tmux-context`（PTY 持久 shell） | headless 每次新进程，PTY 不跨进程存活；ASTRA 已教模型在 bash 里直接用 tmux（容器内进程天然持续，AGENTS.md 已写） |
| `dsh-tool-ask-user` | headless 无人类在场 |
| `dsh-plan-mode` | ASTRA 任务型调度，不需计划模式 |
| `dsh-tool-web`（web_search） | anthropic 模式下 web-search-deepseek 无 `DEEPSEEK_API_KEY` 会 MISSING_CREDENTIAL；靶场内网题不需要外网搜索——**可在 anthropic 模式 patch 里 `disabled: true` 该行**，避免模型误用报错 |

## 五、六小时靶场建议配置快照（Kimi）

```yaml
# dispatch.yaml dsh worker env
env:
  DSH_MODEL: "k3"
  DSH_PROVIDER: "anthropic"
  ANTHROPIC_AUTH_TOKEN: "sk-xxx"
  ANTHROPIC_BASE_URL: "https://api.kimi.com/coding/"
  DSH_PERMISSION_MODE: "danger-full-access"
  DSH_PATCH: "/opt/astra/dsh/astra-headless.patch.yml"   # 含 time-context / MCP 行（按需）
```

```bash
python3 container/astra_runner/runner.py --progress-file %TEMP%\astra-progress.json
```
