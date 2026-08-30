# container — ASTRA 容器与编排

## 镜像构建

```bash
docker build -f container/Dockerfile.slim -t astra-runner .   # 托管出包用 slim；Dockerfile 为本地全量开发镜像
```

镜像内置：Kali 工具链 + f2 逆向链（radare2/r2ghidra/qemu-user/upx/z3 等）、
ASTRA 引擎（server+dispatcher）、**pi**（唯一执行底座）、
`astra-runner` 靶场编排器（默认 ENTRYPOINT）。

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
（pi worker 会话根目录，默认临时目录 astra-pi，worker 子目录按名隔离）。

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
docker build -f container/Dockerfile.slim -t astra-runner .
docker save astra-runner:latest | gzip > agent.tar.gz   # 按平台规范上传
```
