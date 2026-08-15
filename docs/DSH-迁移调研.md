# ASTRA 底层 Worker CLI 迁移调研：claudecode → DeepSeek Harness（dsh）

> 状态：✅ 可行性确认（2026 实测 DSH `@deepseek-ai/dsh@0.1.0-rc.6`）
> 决策：**并存新增 dsh worker 类型**（claudecode 保留可回退）；会话续接采用**自定义 runner 插件暴露 `--session`**。

## 一、项目意图

ASTRA 是 AI 攻防状态空间搜索引擎：`server`（协议真相源）+ `dispatcher`（调度执行器）+ 项目容器（Kali 工具链）+ Worker/Agent CLI 驱动。本次目标是把「星探执行」层的 **claude CLI 替换为 DeepSeek Harness（dsh）**——worker 不再调用 `claude`，而是调用 `dsh` 无头模式。

涉及文件：

| 位置 | 作用 |
|---|---|
| `astra/src/astra/dispatcher/workers/adapters/claudecode.py` | claude 驱动：`claude --session-id <uuid> --dangerously-skip-permissions -p -- <prompt>`；conclude 用 `claude -r <session>` |
| `astra/src/astra/dispatcher/workers/base.py` | `WorkerDriver` 抽象（healthcheck / execute / conclude / session） |
| `astra/src/astra/dispatcher/config.py` | `WorkerType` 字面量 + `WORKER_ENV_KEYS`（claudecode 要 `ANTHROPIC_*`） |
| `astra/src/astra/dispatcher/tasks/reason.py:222` | 审查阶段（challenge/verdict）硬编码 `_claude_executable()` |
| `astra/src/astra/dispatcher/runtime/process.py` | 容器内进程执行（超时/杀进程/输出捕获）——与 CLI 无关，无需改 |
| `container/Dockerfile` | `npm install -g @anthropic-ai/claude-code@2.1.98` 等 |
| `container/astra_runner/astra_runner_engine.py` | 动态生成 dispatch.yaml（claudecode + `api.deepseek.com/anthropic` 兼容端点） |

**核心设计约束**：prompt（如 `explore.md`）明确要求「conclude-phase instruction in the **same session**」——execute 超时/解析失败后，用**同一会话**跑 conclude 抢救模型探索上下文。替换必须保留会话续接能力。

## 二、DSH 能力调研结论（代码 + CLI 实测）

1. **无头模式可用**：`dsh --profile headless "<task>"` 单次运行，把最后一条非空 assistant 文本打到 stdout，`completed` 退出 0 / 错误退出 1——输出契约与 `claude -p` 一致。实测 `--help` 退出 0，profile 自动初始化。
2. **工具链完整**：base bundle 默认挂载 bash（Linux）、fs read/write/edit、glob/grep（ripgrep）、subagent、workflow、web_search 等，满足 ASTRA prompt 的 shell 侦察需求。
3. **模型适配**：原生 `deepseek-official` provider（chat-completions 直连 `api.deepseek.com`），广告模型 `deepseek-v4-flash` / `deepseek-v4-pro`，与 ASTRA 现有模型名一致。
4. **会话续接存在**：会话事件溯源 + JSONL 持久化（`$DSH_HOME/sessions`）；API 有 `sessions.create(id)` 与 `agents.resume()`。headless 未把 resume 暴露成 CLI 参数 → 用 `--patch` 覆盖层/自定义 runner 补上（Phase 2）。
5. **权限与隔离**：`DSH_PERMISSION_MODE=danger-full-access` 等价 `--dangerously-skip-permissions`；`$DSH_HOME` 指定数据根，等价 `CLAUDE_CONFIG_DIR`。
6. **工作区指令兼容**：`dsh-agent-instructions` 原生加载 AGENTS.md/CLAUDE.md，ASTRA 容器工作区已放好，零改动。
7. **跨平台**：Node CLI；Windows 下沿用 `pi.py` 的「node 直跑 + prompt 走 `@file`」先例（DSH bin 为 `lib/bin.js`）。

## 三、映射表（可行性的核心）

| ASTRA 需求 | claude 现状 | DSH 对应 |
|---|---|---|
| 单次执行 | `claude -p -- <prompt>` | `dsh --profile headless "<prompt>"` |
| 会话续接 | `--session-id` / `-r` | `agents.resume()`（需自定义 runner 暴露 `--session`） |
| 跳过权限审批 | `--dangerously-skip-permissions` | `DSH_PERMISSION_MODE=danger-full-access` |
| 模型端点 | `ANTHROPIC_MODEL/BASE_URL/AUTH_TOKEN` | `DEEPSEEK_API_KEY` / `DEEPSEEK_BASE_URL` |
| 配置隔离 | `CLAUDE_CONFIG_DIR` | `DSH_HOME` |
| 健康检查 | curl `/v1/messages` | curl DeepSeek `/models` 或 `dsh --version` |
| 工作区指令 | AGENTS.md | 原生支持 |

## 四、风险与对策

| 风险 | 对策 |
|---|---|
| headless 无 `--session` 参数 | Phase 2：约 100 行 Cordis 插件覆盖 headless startup/runner，有 id → `agents.resume()`，无 → `agents.create()` |
| reason.py 审查阶段硬编码 claude exe | Phase 3：改为优先用 worker 自身 driver，claudecode 分支保留为降级 |
| headless 只打印最后一条 assistant 文本 | bootstrap 增量 JSON 行是单次输出内的多行，行为与 claude -p 一致；端到端实测 + 既有叙述兜底 |
| 每次任务新 Node 进程冷启动（1–3s） | 可接受；并发 8 worker 时留意资源，必要时复用常驻进程 |

## 五、开发计划（Phase 1–5）

- **Phase 1 · dsh 驱动适配器**：✅ 已完成（见下方「Phase 1 落地清单」）。
- **Phase 2 · DSH 侧会话续接扩展**：✅ 已完成（见下方「Phase 2 落地清单」），本机四条路径实测通过（模块解析 / create / create-or-resume 往返 / @file / --help）。
- **Phase 3 · 审查阶段去 claude 硬编码**：✅ 已完成（见下方「Phase 3 落地清单」）。
- **Phase 4 · 容器与编排**：✅ 已完成（见下方「Phase 4 落地清单」）。
- **Phase 5 · 真实验证与验收**：✅ 已完成（见下方「Phase 5 落地清单」）——真实模型（Kimi K3）E2E 4/4 通过。

总工作量约 **3.5–5 天**。

## 六、Phase 1 落地清单（已完成）

- `astra/src/astra/dispatcher/workers/adapters/dsh.py`：`DshDriver(SeedSessionDriver)`——
  - `prepare_session()` 生成 `session-<uuid>`（DSH SessionId 规范形态）
  - `build_execute` / `build_conclude`：`dsh --profile headless [--patch <DSH_PATCH>] [--session <id>] <prompt>`
  - `DSH_RESUME=0` 走无状态模式（不传 `--session`，兼容原版 headless）
  - Windows：node 直跑 `lib/bin.js` 绕过 `.cmd` shim；prompt 走 `@file`（由 Phase 2 定制 startup 展开）
  - healthcheck：curl `{DEEPSEEK_BASE_URL}/chat/completions` + `Authorization: Bearer`
- `config.py`：`WorkerType` 加 `"dsh"`；`WORKER_ENV_KEYS["dsh"] = ("DSH_MODEL", "DEEPSEEK_API_KEY")`
- `workers/registry.py` / `workers/adapters/__init__.py`：注册 `DshDriver`
- `dispatch.example.yaml`：dsh worker 示例（含 DSH_PATCH / DSH_PERMISSION_MODE / DSH_HOME 注释）
- 测试：`test_config_and_adapters.py` 新增 9 个 dsh 用例（配置校验、execute/conclude argv、无状态退化、healthcheck、Windows node 分支），全部通过；全量套件 109 passed。

> 注：`test_pi_driver_models_json_and_execute_argv_include_context_window_and_tools` 在本机失败属既有环境问题（未安装 pi npm 全局包，`shutil.which("pi")` 回退失败），与 dsh 改动无关。

## 七、Phase 2 落地清单（已完成）

- `container/dsh/astra-headless-runner.js`：Cordis 插件（ESM，仅用 dsh 自带依赖）——
  - 解析 `--session <id>` + 任务文本；`-h/--help`；Windows `@<file>` 展开
  - create-or-resume 语义：resume 不到 → **同 id** create（保契约）→ 再失败才退化为全新 id
  - 输出契约与官方 headless 一致：最后一条 assistant 文本 → stdout，completed 退 0 / error 退 1
- `container/dsh/astra-headless.patch.yml`：`--patch` 覆盖层——disable 官方
  headless-startup/headless-runner，insert 自定义 runner（裸说明符
  `@deepseek-ai/dsh/lib/astra-headless-runner.js`，与官方 `.../startup` 同一解析规则）
- `container/dsh/README.md`：安装（复制 runner 进 dsh 包 lib/）、用法、验证记录

**机制验证结论（本机实测，无真实 key）**：
1. 模块解析：裸说明符从 dsh 安装成功加载；绝对路径方案（`E:/...`）因 launcher 未传
   `bareModuleBaseUrl` 而失败（`E:` 被当 URL scheme）——故采用裸说明符 + 复制进 dsh 包
2. create 路径：无 `--session` 直达 LLM 层（无 key 报 `MISSING_CREDENTIAL`）
3. **create-or-resume 往返**：`--session session-contract-001` 首跑同 id 创建并落盘
   `$DSH_HOME/sessions/--<cwd>--/session-contract-001/`；二跑同 id → `resumed session`——
   execute→conclude 的会话契约机制完整成立
4. `@file` 展开、`--help` 正常

> 待真实 `DEEPSEEK_API_KEY` 环境做模型级 E2E：execute 跑命令 → conclude 复述，
> 确认模型能看到 execute 的探索上下文（命令见 container/dsh/README.md）。

## 八、Phase 3 落地清单（已完成）

- `workers/base.py`：`WorkerDriver` 新增 `supports_review() -> bool`（默认 True）——
  审查阶段（challenge/verdict）对输出契约稳定性要求高，个别驱动可声明不支持
- `workers/adapters/pi.py`：`PiDriver.supports_review() -> False`（实测 pi 审查偶发提前退出）
- `tasks/reason.py`：
  - `_resolve_review_worker(config, worker)`：审查 worker/driver 选择——优先产生提案的
    worker 自身 driver；不支持审查（pi）则回退到配置中的 claudecode worker（driver 构造
    命令，非硬编码可执行文件）；无回退则用自身并告警（重试+降级放行兜底）
  - `_run_review_stage`：删除 `_claude_executable` 硬编码分支，命令一律
    `driver.prepare_session() + driver.build_execute()`（与主任务链路一致）
- 测试：+5（pi 声明不支持 / resolve 三路径 / 审查命令走 driver），全量 114 passed

> 历史修复保真：重构备忘 #15 的"审查用 pi 偶发提前退出→改用 claude"通过
> `supports_review` 能力位 + claudecode worker 回退保留，同时消除了对
> `_claude_executable()` 的硬编码依赖——claudecode/dsh/codex 的审查命令
> 全部由其自身 driver 构造，env 使用回退 worker 自身的配置。

## 九、Phase 4 落地清单（已完成）

- `container/astra_runner/astra_runner_engine.py`：
  - `_render_dispatch_config` 支持 `ASTRA_WORKER_TYPE`：`claudecode`（默认，行为不变）/
    `dsh`；共享 yaml 骨架
  - `_render_dsh_worker`：生成 dsh worker 配置——`DSH_MODEL` / `DEEPSEEK_API_KEY` /
    可选 `DEEPSEEK_BASE_URL` / `DSH_PERMISSION_MODE=danger-full-access` /
    `DSH_HOME`（默认临时目录按 worker 隔离）/ `DSH_PATCH`
  - `_resolve_dsh_patch`：env `DSH_PATCH` 优先 → 仓库 `container/dsh/`（本地联调）→
    镜像 `/opt/astra/dsh/astra-headless.patch.yml` 兜底
- `container/Dockerfile`：`npm install -g @deepseek-ai/dsh@0.1.0-rc.6` +
  `COPY ./container/dsh /opt/astra/dsh` + 复制 runner 进 dsh 包 `lib/`
- `container/README.md`：worker 选择表（claudecode/dsh）+ dsh 前置条件
- 测试：+3（dsh 配置生成且过 `DispatchConfig.load` 校验 / 缺 key 报错 / 默认 claudecode
  不回归），全量 117 passed

## 十、Phase 5 落地清单（已完成，真实模型验证）

**关键发现：DSH 可用 dormant `llm-pi-ai` 适配器纯配置讲 Anthropic Messages 协议**
（`supportedProtocols` 含 anthropic）——用户的真实端点（Kimi `api.kimi.com/coding`、
dispatch.example.yaml 的 DeepSeek `/anthropic` 兼容路径、任意 claude 兼容网关）
全部可直接接入，无需 DeepSeek key、无需新代码。实测链路：
`dsh headless → astra runner → dsh-agent → llm-pi-ai(anthropic) → Kimi /v1/messages → K3`。

**真实模型 E2E（本机 Windows + Kimi K3，DSH 0.1.0-rc.6，e2e_verify.py 4/4 PASS）**：
1. ✅ 基础调用：`PONG` exit=0
2. ✅ execute：模型执行 `echo ASTRA_E2E_MARKER_42` 并返回其输出
3. ✅ **会话续接**：同 session 二次调用 `resumed session`，模型不执行命令即复述出
   `ASTRA_E2E_MARKER_42`——ASTRA execute→conclude 双阶段契约跨进程成立
4. ✅ 落盘：`$DSH_HOME/sessions/--<cwd>--/session-e2e-verify-001/session.jsonl.zstd`

**代码/配置交付**：
- `container/dsh/astra-headless.patch.yml`：升级为 env 驱动——`DSH_PROVIDER` 选择
  deepseek-official / anthropic 路由；anthropic 模式经 `!!js` 表达式读
  `ANTHROPIC_AUTH_TOKEN`/`ANTHROPIC_BASE_URL`/`DSH_MODEL`
- `dsh.py`：`DshDriver` 支持 `DSH_PROVIDER=anthropic`——healthcheck 走
  `{ANTHROPIC_BASE_URL}/v1/messages` + `x-api-key` + `anthropic-version`
- `config.py`：dsh 凭据按 provider 分派校验（deepseek → DEEPSEEK_API_KEY；
  anthropic → ANTHROPIC_AUTH_TOKEN）
- `astra_runner_engine.py`：`DSH_PROVIDER` env 生成对应 worker env 块
- `dispatch.example.yaml`：双模式示例
- `container/dsh/e2e_verify.py`：验收脚本（--provider deepseek|anthropic，
  4 项检查自动判定）
- 测试：+6（anthropic 配置校验 / anthropic healthcheck / describe env 展开 /
  deepseek 回归 / 引擎 anthropic 渲染×2），全量 123 passed

> 已知坑：YAML 普通标量不能含 `: `——patch 内三元表达式需加引号
> （`provider: !!js "..."`）；模型 id 用 wire 名（Kimi 为 `k3`，claude 配置里的
> `k3[1M]` 是内部映射）。
