# container — ASTRA 容器与编排

## 镜像构建

```bash
docker build -f container/Dockerfile -t astra-runner .   # 构建目录为仓库根
```

镜像内置：Kali 工具链、ASTRA 引擎（server+dispatcher）、模型 CLI（claude/codex/pi/
**dsh**）、`astra-runner` 靶场编排器（默认 ENTRYPOINT）。

## Worker 选择（astra-runner 本地/托管模式）

`container/astra_runner/runner.py` 的引擎（`astra_runner_engine.py`）根据环境变量
生成 dispatch.yaml，`ASTRA_WORKER_TYPE` 选择 worker：

| ASTRA_WORKER_TYPE | 需要的 env | 说明 |
|---|---|---|
| `claudecode`（默认） | `ANTHROPIC_MODEL` / `ANTHROPIC_BASE_URL` / `ANTHROPIC_AUTH_TOKEN` | claude CLI + DeepSeek Anthropic 兼容端点 |
| `dsh` | `DSH_MODEL`（默认 deepseek-v4-flash）/ `DEEPSEEK_API_KEY` | DeepSeek Harness 无头模式，会话续接由 `container/dsh/` 扩展提供 |

dsh 模式可选 env：`DEEPSEEK_BASE_URL`（默认官方地址）、`DSH_PATCH`（默认镜像内
`/opt/astra/dsh/astra-headless.patch.yml`）、`DSH_HOME`（默认临时目录按 worker 隔离）。

示例：

```bash
ASTRA_WORKER_TYPE=dsh \
DEEPSEEK_API_KEY=sk-xxx \
DSH_MODEL=deepseek-v4-pro \
python3 container/astra_runner/runner.py
```

## dsh 前置条件

1. 镜像内已装 `@deepseek-ai/dsh` 并把 `container/dsh/astra-headless-runner.js`
   复制进 dsh 包 `lib/`（Dockerfile 已处理）；
2. 本地 Windows 联调需手动安装（见 `container/dsh/README.md`）。

## 托管模式

```bash
docker build -f container/Dockerfile -t astra-runner .
docker save astra-runner:latest | gzip > agent.tar.gz   # 按平台规范上传
```
