# container/dsh — ASTRA 定制的 DeepSeek Harness headless 扩展

为 `dsh --profile headless` 增加 **会话续接** 与 **Anthropic 协议模型路由**，
支撑 ASTRA 的 execute→conclude 双阶段模式（对应 claude 的 `--session-id` / `-r`）。

## 文件

| 文件 | 作用 |
|---|---|
| `astra-headless-runner.js` | Cordis 插件（ESM）：`--session <id>` 解析、create-or-resume、`@file` 展开 |
| `astra-headless.patch.yml` | `--patch` 覆盖层：禁用官方 headless-startup/runner，插入自定义 runner；挂载 llm-pi-ai 的 anthropic 路由（env 驱动） |
| `e2e_verify.py` | 端到端验收脚本（真实模型凭据一键跑：基础调用 / execute / 会话续接 / 落盘） |

## 原理

- 官方 headless 只支持一次性新会话（`agents.create()`，随机 `session-<uuid>`）。
- 本扩展的 runner 逻辑：
  - 无 `--session`：与官方一致，新建随机会话；
  - 有 `--session <id>`：先 `agents.resume()` 恢复持久化会话（等价 `claude -r`）；
    会话不存在则用**同一 id** `agents.create()`（等价 `claude --session-id` 的
    create-or-resume 语义），保证 ASTRA 驱动派发的 id 在 conclude 阶段一定能续上；
    仅当同 id 创建也失败（磁盘状态异常）才退化为全新 id。
  - Windows 约定：任务以 `@<file>` 传入时自动读取文件内容。
- 会话持久化在 `$DSH_HOME/sessions`（JSONL），跨进程可恢复——ASTRA 每个 worker
  用独立 `DSH_HOME` 目录隔离（等价 claude 的 `CLAUDE_CONFIG_DIR`）。
- 模型路由（`DSH_PROVIDER` env 选择，patch 内 `!!js` 表达式启动时求值）：
  - `deepseek`（默认）：`deepseek-official` 路由（官方 chat-completions，`DEEPSEEK_API_KEY`）
  - `anthropic`：`llm-pi-ai` 的 anthropic 路由（Anthropic Messages 协议，
    `ANTHROPIC_AUTH_TOKEN` / `ANTHROPIC_BASE_URL`）——适配 Kimi /
    DeepSeek `/anthropic` 兼容端点 / 任意 claude 兼容网关，DSH 无需改代码即可替换 claude CLI

## 安装

runner 必须位于 dsh 包内（`name` 用裸说明符 `@deepseek-ai/dsh/lib/astra-headless-runner.js`，
与官方 `@deepseek-ai/dsh-headless/startup` 同一解析规则），因此需要把 JS 复制进 dsh 包：

```bash
# 容器（Dockerfile，kali 用户下 sudo）
sudo npm install -g @deepseek-ai/dsh@0.1.0-rc.6
sudo cp /opt/astra/dsh/astra-headless-runner.js "$(npm root -g)/@deepseek-ai/dsh/lib/astra-headless-runner.js"

# 本机 Windows（npm 全局安装后，npm root -g 即 %APPDATA%\npm\node_modules）
npm install -g @deepseek-ai/dsh@0.1.0-rc.6
copy container\dsh\astra-headless-runner.js "%APPDATA%\npm\node_modules\@deepseek-ai\dsh\lib\astra-headless-runner.js"
```

## 用法

```bash
# 新建会话执行任务（deepseek 模式）
DSH_PROVIDER=deepseek DSH_MODEL=deepseek-v4-pro DEEPSEEK_API_KEY=sk-xxx \
  dsh --profile headless --patch /opt/astra/dsh/astra-headless.patch.yml "task"

# 复用会话（execute→conclude 双阶段）
... dsh --profile headless --patch ... --session session-<uuid> "task"

# anthropic 模式（Kimi 示例）
DSH_PROVIDER=anthropic DSH_MODEL=k3 \
  ANTHROPIC_AUTH_TOKEN=sk-xxx ANTHROPIC_BASE_URL=https://api.kimi.com/coding/ \
  dsh --profile headless --patch /opt/astra/dsh/astra-headless.patch.yml "task"
```

ASTRA 侧由 `dsh` worker 驱动（`astra/src/astra/dispatcher/workers/adapters/dsh.py`）
自动构造上述命令；dispatch.yaml 的 worker env（deepseek 模式）：

```yaml
env:
  DSH_MODEL: "deepseek-v4-pro"
  DSH_PROVIDER: "deepseek"
  DEEPSEEK_API_KEY: "sk-xxx"
  DSH_PATCH: "/opt/astra/dsh/astra-headless.patch.yml"   # 指向本补丁
  DSH_PERMISSION_MODE: "danger-full-access"             # 等价 --dangerously-skip-permissions
  DSH_HOME: "/tmp/astra-dsh/<worker-name>"              # 会话/凭据隔离目录
```

anthropic 模式把 `DEEPSEEK_API_KEY` 换成 `ANTHROPIC_AUTH_TOKEN`（+ 可选
`ANTHROPIC_BASE_URL`），并设 `DSH_PROVIDER: "anthropic"`。

## 端到端验收

```bash
# anthropic 模式（Kimi）
python3 container/dsh/e2e_verify.py --provider anthropic --model k3 \
  --api-key sk-xxx --base-url https://api.kimi.com/coding/

# deepseek 模式
python3 container/dsh/e2e_verify.py --provider deepseek --model deepseek-v4-flash \
  --api-key sk-xxx
```

脚本验证：① 基础调用真实模型返回 ② execute 让模型跑命令并返回输出
③ **conclude 同 session 复述前一阶段输出（不执行命令）**——ASTRA 双阶段契约
④ 会话落盘 `$DSH_HOME/sessions`。

## 验证记录（本机 Windows，DSH 0.1.0-rc.6，真实 Kimi K3）

- 模块解析：裸说明符从 dsh 安装解析成功。
- create-or-resume：首跑同 id 创建并落盘 → 再跑同 id `resumed session`。
- **模型级 E2E（4/4 PASS）**：`PONG` / execute 返回 `ASTRA_E2E_MARKER_42` /
  conclude 复述 `ASTRA_E2E_MARKER_42` / 会话落盘。
- anthropic 模式：DSH → llm-pi-ai → Kimi `/v1/messages` 全链路真实调用成功；
  模型 id 用 wire 名（Kimi 为 `k3`，claude 配置里的 `k3[1M]` 是内部映射）。
- `@file` 展开与 `--help` 正常。
