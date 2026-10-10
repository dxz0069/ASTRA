# REA 静态 JS 试点使用说明

本试点把独立安装的 REA 接入 ASTRA CLI，分析本地 JavaScript/Electron 目录并返回有界的静态候选摘要。当前接入不执行目标应用，不自动创建星图事实或 finding。冻结样本的工具评测见 [rea-pilot-results.md](rea-pilot-results.md)，试点范围见 [rea-pilot-plan.md](rea-pilot-plan.md)。这些工具结果尚不能证明模型解题率或比赛成绩提升。

## 默认关闭与依赖

只有 `ASTRA_STATIC_JS_ENABLED` **精确等于字符串 `1`** 时才能分析或读取保存的视图。未配置、`0`、`true` 等值均保持关闭。查看 CLI 帮助无需启用，导入 CLI 和查看帮助不会加载分析器模块。

执行分析还需要：

- 当前环境的 `PATH` 中有可用的 Node.js。
- 独立安装并准备好 REA 的依赖和构建产物。
- `ASTRA_STATIC_JS_CLI` 是该环境中已安装的 REA `.mjs` 或 `.js` 入口的**绝对文件路径**，不是命令名、目录或 npm shim。适配器通过 Node 调用入口的 `analyze-javascript-application DIRECTORY --json` 命令。
- 当前环境中已经安装本版本的 ASTRA CLI，或通过本仓库的 `uv run --project astra astra ...` 调用。

入口存在只说明路径可见；安装是否完整仍需核对入口、依赖和实际 CLI 执行结果。ASTRA 不自动下载分析器，也不把上游源代码、`node_modules` 或构建产物打包进本仓库。

### 本机 PowerShell 示例

在包含待分析目录的工作区运行；下面的 REA 路径是本次试点使用的独立本地安装路径，其他机器需替换。

```powershell
$env:ASTRA_STATIC_JS_ENABLED = '1'
$env:ASTRA_STATIC_JS_CLI = 'E:\study\项目\research\rea-research\scripts\rea.mjs'
astra static-js .\js-app
```

从 ASTRA 仓库运行而未安装全局命令时：

```powershell
uv run --project astra astra static-js .\js-app
```

`DIRECTORY` 必填，分析和读取保存视图互斥。成功时 stdout 为一行紧凑 JSON，包含 `run_id`、覆盖状态、候选及来源位置；失败时返回非零退出码和 Click 错误信息。

## 按文件读取保存的视图

先从分析结果取得 `run_id`，再在同一工作区请求相对于被分析目录的文件路径。`--run-id` 与 `--file` 必须同时提供，此模式不接受 `DIRECTORY`。

```powershell
$result = astra static-js .\js-app | ConvertFrom-Json
$runId = $result.run_id
astra static-js --run-id $runId --file src/app.js
$page = astra static-js --run-id $runId --file src/app.js --view application --offset 0 --limit 10 | ConvertFrom-Json
if ($null -ne $page.next_offset) {
    astra static-js --run-id $runId --file src/app.js --view application --offset $page.next_offset --limit 10
}
astra static-js --run-id $runId --file src/app.js --view semantic --offset 0 --limit 10
```

| 参数 | 默认值 | 范围与用途 |
|---|---|---|
| `--view` | `application` | `application` 请求应用结构候选；`semantic` 请求语义图中的静态观察、关系和未知状态 |
| `--offset` | `0` | 非负整数；对当前视图中的每类列表使用同一偏移 |
| `--limit` | `10` | 整数 `1–40`，单次请求上限为 `40` |
| `--file` | 无 | 使用相对于分析输入目录的 `/` 分隔路径，如 `src/app.js`；不接受绝对路径、反斜杠或 `..` 遍历 |

列表还可能因总输出预算被裁剪，应使用返回的 `next_offset` 继续翻页，而不是假设下页偏移恒为当前 `offset + limit`；两个视图分别翻页。结合返回的总数、省略信息和 coverage 判断是否需要继续读取。空页不能证明文件没有风险。保存视图依赖本地证据仍然可用，`run_id` 不是远端服务地址。

## Pi worker 与 Docker

Pi 的试点使用提示仅由该 worker 的 `worker.env` 中 `ASTRA_STATIC_JS_ENABLED` 是否精确等于字符串 `1` 控制。在主机 shell 设置变量不等于为每个 Pi worker 显式启用了提示。`ASTRA_STATIC_JS_CLI` 是实际执行分析时的必需配置；未提供有效入口时，分析命令会报错，但提示仍可能出现。提示只是告知可选命令；是否实际分析由任务和目录决定。

下面是合并到现有 worker 配置的片段，不是完整 dispatcher 配置；其余模型、密钥、任务类型等沿用现有配置。

```yaml
workers:
  - name: existing-pi-worker
    type: pi
    env:
      ASTRA_STATIC_JS_ENABLED: "1"
      ASTRA_STATIC_JS_CLI: "/opt/rea/scripts/rea.mjs"
```

Docker worker 需要在**容器内部**分别安装 Node.js、REA 及其依赖/构建产物、本版本 ASTRA CLI。路径必须在容器内可见，例如 `/opt/rea/scripts/rea.mjs`；主机 Windows 路径不能直接用于 Linux 容器。容器的当前工作区还需要有待分析的可信目录。只设置这两个变量不能完成这些安装。

## 输入、快照与执行边界

输入必须是**可信且保持静止的工作区目录**。相对路径按命令当前工作目录解析，绝对路径也必须位于该工作区内；路径中不能含 `..`。预算统计输入树中的所有普通文件，未默认忽略 `node_modules` 等目录。

分析前复制文件字节到临时目录并记录 SHA256，REA 读取这份快照。适配器检查来源路径及复制过程中的文件身份、大小和修改时间，发现变化就拒绝；Windows 的链接/reparse 复查仍不能替代操作系统隔离，也不保证抵御并发恶意改写。分析期间不要修改输入、依赖或入口。

以下输入拒绝处理：

- 符号链接、Windows junction/reparse point，包括输入目录链中的此类路径。
- 普通文件与目录以外的特殊文件。
- 名称以 `.asar` 或 `.asar.unpacked` 结尾的文件或目录。

试点不启动目标 JavaScript、Electron renderer、bundle 或 native addon。分析器本身会作为 Node 子进程运行；超时和输出上限是资源边界，**此适配器不是 OS 沙箱**。分析器仍受运行账户的权限约束。

## 资源预算

| 项目 | 上限 |
|---|---:|
| 输入普通文件数 | 500 |
| 输入文件总字节数 | 64 MiB |
| 单个文件字节数 | 16 MiB |
| 目录深度 | 16，输入根为深度 0 |
| 遍历的目录 entry 总数 | 2,000，包含目录和文件 |
| 分析器子进程执行预算 | 30 秒 |
| 分析器 stdout | 16 MiB |
| 分析器 stderr | 256 KiB |
| CLI 分析摘要或单个 inspect 视图 | 12 KiB，按 UTF-8 字节计，包含结尾换行 |

输入暂存、JSON 解析与进程清理有额外成本，因此 30 秒不表示整条 CLI 命令总耗时必然小于 30 秒。超过限制、解析失败或分析器错误均作为失败处理，不能解释成“未发现问题”。

## 证据与结果使用

分析结果提供原始证据与 manifest 的本地保存路径。manifest 用于记录原始目录、来源文件与暂存阶段的映射、文件字节数和哈希；证据哈希用于核对保存数据与这次分析一致。哈希和路径映射**不证明分析器、依赖或供应链可信**。

原始 REA JSON 可能包含 URL 参数、路径、令牌样式字符串或其他敏感值。送入 agent 的摘要/inspect 视图仅对字符串做有限的裁剪和尽力脱敏，不是完整的秘密识别或数据隔离机制。原始证据应留在本地受控位置，不应整份直接注入 Pi 上下文或公开提交。

候选必须保留 `observed`、`inferred`、`unknown`、`unavailable`、`partial`、`truncated` 等状态。`observed` 在本工具中表示从静态工件观察到的结构，不能解读为行为已经运行；应用结构覆盖完整也不代表语义覆盖完整。

用来源位置回读源码、核对歧义并设计独立验证。只有在授权范围内取得可复现的行为和影响证据后，才能形成已验证的 finding；静态路径、IPC 配对、敏感 API 或字符串命中本身均不满足这个要求。
