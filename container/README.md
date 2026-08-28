# container — ASTRA 容器与编排

## 镜像构建

```bash
docker build -f container/Dockerfile.slim -t astra-runner .   # 托管出包用 slim；Dockerfile 为本地全量开发镜像
```

镜像内置：Kali 工具链、ASTRA 引擎（server+dispatcher）、**claude-code CLI**、
`astra-runner` 靶场编排器（默认 ENTRYPOINT）。

## Worker 选择（astra-runner 本地/托管模式）

`container/astra_runner/runner.py` 的引擎（`astra_runner_engine.py`）根据环境变量
生成 dispatch.yaml。2026-08-28 起**仅 claudecode**（dsh/DeepSeek Harness 已整体
移除——tsecbench 前十 0 家使用，6 家 Claude Code/Agent SDK，见
`docs/Tsecbench前十名日志机制拆解.md` §6）。

| ASTRA_WORKER_TYPE | 需要的 env | 说明 |
|---|---|---|
| `claudecode`（默认） | `ANTHROPIC_MODEL/BASE_URL/AUTH_TOKEN`（DS 通道）+ 可选 `ZHIPU_API_KEY/ZHIPU_ANTHROPIC_BASE_URL/ZHIPU_MODEL`（GLM 通道，端点默认 https://open.bigmodel.cn/api/anthropic） | 双通道混合：DS explore×2 + GLM explore×2 + GLM reason×2；仅 DS key 时单通道 |

可选 env：`ASTRA_EXPLORE_REPLICAS`（默认 2）、`ASTRA_EXPLORE_MAXRUN`（默认 3）、
`ASTRA_CLAUDE_HOME`（CC 会话/配置根目录，默认临时目录 astra-claude，worker 子目录
按名稳定复用——引擎重启后 `claude -r` 仍能续接会话）。

示例：

```bash
ANTHROPIC_AUTH_TOKEN=sk-xxx \
ANTHROPIC_MODEL=deepseek-v4-flash \
ANTHROPIC_BASE_URL=https://api.deepseek.com/anthropic \
python3 container/astra_runner/runner.py
```

MCP：引擎启动时向每个 worker 的 CLAUDE_CONFIG_DIR 写入 `.claude.json`，注册
playwright MCP（镜像内全局安装的 `playwright-mcp`，Web 类题目真实浏览器）。

## 托管模式

```bash
docker build -f container/Dockerfile.slim -t astra-runner .
docker save astra-runner:latest | gzip > agent.tar.gz   # 按平台规范上传
```
