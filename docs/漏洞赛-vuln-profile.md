# 智能漏洞挖掘专项：vuln Pi profile

这个 profile 是第三届智能漏洞挖掘专项的本地实现切片。它把“能不能发请求”从模型提示词收紧为配置和工具边界，方便在资产清单发布后对照 TSRC 授权逐项配置。

最小模拟配置见 [`astra/examples/vuln-local.example.yaml`](../astra/examples/vuln-local.example.yaml)。其中模型端点与目标端点均是本地占位值，必须由实际 loopback 模拟服务提供；没有这些服务时只验证配置，不会得到任务结果。

## 启用条件

- `PI_TOOL_PROFILE=vuln` 必须提供完整 `ASTRA_VULN_SCOPE_JSON`：项目码、UTC 窗口、精确 origin、路径前缀和方法。
- 生产运行必须显式设置 `ASTRA_VULN_LOCAL_TEST=0`，`PI_BASE_URL` 必须等于显式的 `ASTRA_VULN_GATEWAY_URL` HTTPS 地址。
- 本地 loopback 模拟必须使用 `ASTRA_VULN_LOCAL_TEST=1` 和 `runtime.execution=local`；此模式只允许 loopback 目标，不能用于比赛资产。
- vuln worker 不能和未收紧的 worker 混用，并且只使用 `prompts/vuln`。
- 服务端与 dispatcher 进程设置同一个 `ASTRA_EVIDENCE_COLLECTOR_TOKEN`，不要放入 worker 的 `env`。在专项服务端设置 `ASTRA_VULN_STRICT=1`，使高价值 Finding 必须有来源工件，并在完成项目的写事务内复查未结步骤与待核验 Finding。dispatcher 的 vuln Decide 也会先读取最新星图再提出完成。

## 工具边界

Pi 以 `--no-extensions --no-mcp --tools read,scoped_request` 启动，再显式加载 `vuln_scope.js`。模型看不到 bash、powershell、write、edit 等工具。扩展在每次调用和每个重定向上检查 scope、时间、方法、路径、HTTPS、DNS 解析结果；限制响应大小、超时和每次 Pi 运行的请求数量，并返回响应 SHA-256。

“每次 Pi 运行 20 个请求”是进程内预算。续会话或重新启动会重置它；跨运行的全局速率、总量配额和平台网关会话计数仍需由调度器/平台实现，不能把这个切片当作完整网络出口隔离。

## 候选身份与完成条件

Execute 可以为 Finding 提供六项结构化身份：`asset_origin`、`entry_point`、`category`、`root_cause`、`impact`、`conditions`。六项齐全时才按精确指纹合并待核验或已确认的同一问题；不同影响或条件保留为不同候选。缺身份的旧候选仅沿用“待核验且描述规范化后完全一致”的旧去重保护，不与有结构身份的记录合并。

完成检查分别判断未结 Step、待核验或 blocked 的高价值 Finding；只有已知工作状态清楚，Decide 才能提交完成。`check_lane_completion` 是后续专项产物的纯函数合同；当前 ASTRA 尚无持久的 lane run/artifact 表，不能声称已具备多赛道完成覆盖率。

## 生产使用前检查

1. 资产清单公布后，逐项核对 TSRC 授权、路径和允许方法，生成最小 scope。
2. 使用平台模型网关并保存真实网关会话 ID；本地 Pi session ID 不是平台 trace。
3. 先在许可的 mock/loopback 环境跑回归，再做低影响侦察；发现候选后由 Strike 独立验证，参赛者人工复现后再提交 TSRC。
4. 如果调度器、容器或宿主机仍有其他网络出口，禁止把 `vuln` profile 宣称为出口闸门，先补充相同 scope 的网络层策略。
