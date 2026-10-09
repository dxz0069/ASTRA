# container — ASTRA 容器与编排

## 镜像构建

```bash
docker build -f container/Dockerfile -t astra-runner .
```

`Dockerfile.slim` 当前依赖本机已有的 `astra-slim:v5s` 镜像。它是旧缓存构建路径，
干净环境无法直接构建；现阶段参赛包以不依赖本地预置镜像的完整 Dockerfile 为准，
并需实际构建验证。

镜像内置：Kali 工具链 + f2 逆向链（radare2/r2ghidra/qemu-user/upx/z3 等）、
ASTRA 引擎（server+dispatcher）、**pi**（唯一执行底座）、
`astra-runner` 靶场编排器（默认 ENTRYPOINT）。

Pi 固定为 `@earendil-works/pi-coding-agent@1.1.0`，要求 Node ≥ 22.19.0。
两个 Dockerfile 均启用 npm `--engine-strict` 并在安装后校验 `pi --version`；
基础镜像的 Node 版本不满足要求时构建会失败。本地安装使用：

```bash
npm install -g --engine-strict @earendil-works/pi-coding-agent@1.1.0
pi --version  # 应输出 1.1.0
```

## Worker 选择（astra-runner 本地/托管模式）

`container/astra_runner/runner.py` 的引擎（`astra_runner_engine.py`）根据环境变量
生成 dispatch.yaml。v0.2 星图架构重建（2026-08-29）起**仅 pi**——完全可控的
极简 Agent Loop，任务面收敛为 bootstrap / execute / decide 三类（claudecode 与
dsh 栈均已移除）。

| ASTRA_WORKER_TYPE | 需要的 env | 舰队形态 |
|---|---|---|
| `pi`（默认且唯一） | `PI_API_KEY/PI_BASE_URL/PI_MODEL/PI_PROVIDER_API`（DS 执行通道，provider 必须为 `anthropic-messages`）+ 可选 `ZHIPU_API_KEY/ZHIPU_PI_BASE_URL/ZHIPU_PI_MODEL/ZHIPU_PI_PROVIDER_API`（GLM 决策通道） | deepseek-execute×N（p0，bootstrap+execute）+ glm-decide（p1，decide；无 GLM key 时 deepseek-decide 兜底） |

可选 env：`ASTRA_EXECUTE_REPLICAS`（默认 4）、`ASTRA_EXECUTE_MAXRUN`（默认 3，
r5 实测最优拓扑 4×3）、`ASTRA_DECIDE_TIMEOUT`（默认 600s）、`ASTRA_PI_HOME`
（pi worker 会话根目录，默认临时目录 astra-pi，worker 子目录按名隔离）、
`ASTRA_MODEL_RETRY_MAX`（外层瞬时模型错误退避重试次数，默认 2，0=关闭）。
Pi 1.1.0 自带有限的内层重试和上下文压缩恢复；ASTRA 外层重试针对 Pi 最终仍未恢复的
传输或瞬时服务错误，包括托管网关 SSE 断流 "incomplete SSE response"。

ASTRA 继续通过 Pi 的 JSON 模式运行阶段，等进程与输出流完整收尾后读取最终消息、
工具证据和用量，确保 Pi 内层重试或压缩恢复完成后再判定阶段结果。

### Pi 工具 profile

`PI_TOOL_PROFILE` 控制 Pi 暴露给模型的内置工具集合，默认值为 `minimal`。这是
worker 级别的环境变量，可在 `dispatch.yaml` 的每个 `pi` worker `env` 中设置：

| profile | bootstrap / execute | decide / challenge | 适用场景 |
|---|---|---|---|
| `minimal`（默认） | `read,write,bash,ls` | `read` | 日常比赛运行；执行阶段保留靶场操作能力，决策阶段只读图快照 |
| `full` | `read,write,edit,bash,grep,find,ls` | `read` | 兼容 bootstrap 和 execute 的旧配置或调试工具选择问题；决策与质询仍固定只读 |

`minimal` 只收敛 Pi 的工具 schema，不限制容器内已有的命令。执行阶段仍可通过
`bash` 调用 `rg`、`find`、`sed` 等命令，并用 `write` 保存脚本和证据；决策阶段
使用 `read` 读取 `/tmp/astra-prompts/<phase>-<id>/graph.yaml`。未知值会被配置校验
拒绝；未设置时按 `minimal` 处理。需要逐步回退旧行为时，将对应 worker 的
`PI_TOOL_PROFILE` 改为 `full`，无需切换 Pi 或模型；`decide` 与 `challenge` 仍通过
专用只读调用路径固定为 `read`，避免兼容开关放宽审查边界。

Pi 默认以 `--no-skills --no-context-files` 启动。构建镜像不再复制旧 `.agents`
技能目录和 `AGENTS.md`；本地执行同样不自动种入这些文件。自建环境可用
`ASTRA_WORKSPACE_SEED` 把它们复制进工作区，但 Pi 不会自动加载，只有显式读取时才生效。

托管镜像默认不复制 `container/knowledge` 中按旧题码索引的解题笔记，也不在
工作区放置第二份副本。原始资料保留在本地仓库供复盘；若自建环境确认需要，
可通过 `ASTRA_KNOWLEDGE_FILE` 显式指定外部知识库文件。缺省文件不存在时，
runner 按空知识库运行。

Pi 阶段用量默认写结构化日志，包含项目/步骤、阶段、耗时、进程结果和逐轮累加的
input/output/cacheRead token；设 `ASTRA_PHASE_USAGE_JSONL` 可另外写入 JSONL。
缺失的 usage 会标记为不完整，记录中不包含提示词、密钥或工具输出。

示例（本地跑，完整配方见 `dist/local-fgs-run.env` + 启动脚本 `dist/run-local.sh`）：

```bash
set -a; . dist/local-fgs-run.env; set +a
unset ANTHROPIC_AUTH_TOKEN ANTHROPIC_BASE_URL ANTHROPIC_API_KEY ANTHROPIC_MODEL
astra/.venv/Scripts/python.exe container/astra_runner/runner.py \
  --progress-file dist/astra-progress-<轮次>.json --watchdog
```

环境注意：shell 预置的 `ANTHROPIC_*` 变量会被 pi 继承，必须显式 unset；
`worker_healthcheck` 已在引擎渲染的 yaml 中固定 disabled（pi LLM 冷启动首调
可超 70s，健康检查必杀）。

## 托管模式

```bash
docker build -f container/Dockerfile -t astra-runner .
docker save astra-runner:latest | gzip > agent.tar.gz   # 按平台规范上传
```
