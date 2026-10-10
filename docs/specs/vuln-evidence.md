# 漏洞 Finding 证据合同

高价值 Finding 是待核验线索。`verification_status` 记录 AI Strike 的判断；`human_reproduction_status` 是单独的人工复现状态，模型可调用的 Finding 和 conclude API 均不能修改它。旧数据的人工状态默认为 `not_started`，旧的 `confirmed` 记录不会凭迁移获得证据。

## 采集与归属

`scoped_request` 工具的真实执行事件由 dispatcher 匹配 `tool_execution_start` 和成功的 `tool_execution_end` 后导入。`POST /projects/{project_id}/steps/{step_id}/evidence` 需要单独的 `ASTRA_EVIDENCE_COLLECTOR_TOKEN`，该凭据由 dispatcher 进程持有，不传给 Pi worker。服务端要求 step 正在执行且已被认领，使用 step 的 worker 作为 owner。模型正文、URL、自己填写的摘要和未经配对的事件都不能生成受信证据。

专项服务端启用 `ASTRA_VULN_STRICT=1` 后，高价值 Finding 的首次结论也必须引用当前 Step 的已导入工件；完成项目时会在同一写事务中复查未结工作。生产部署需同时配置 collector token 和 strict 标志。

每条导入记录有服务端生成的 `ev_...` ID、`astra://projects/.../evidence/...` URI、项目与 step、Pi session ID、工具调用 ID、采集时的 scope SHA256 引用、请求与响应元数据、实际响应体。服务端规范化并持久化工件内容，计算 `sha256` 和 `body_sha256`。`GET /projects/{project_id}/evidence/{id}` 返回工件，可重新计算摘要。重复导入同一 `(project, step, session, tool_call_id)` 且内容相同时返回原记录；不同内容拒绝。新会话可以重用工具调用 ID。

`scope_sha256` 目前只是采集事件所使用 scope 文本的版本引用。服务端尚未持久化权威 scope manifest，也没有将这个摘要与授权配置核对，因此此字段**不能证明目标已获授权**。collector token 隔离与 Pi 事件配对能阻止普通模型文本直接伪造证据，但无法证明受信执行器本身未被篡改，也无法仅凭一条 HTTP 响应证明漏洞影响。

## Finding 与 Strike

Execute 的结论可提交 `evidence_refs`，由 dispatcher 将本轮工具调用 ID 解析为成功导入的证据 ID；Finding 保存第一条 `source_evidence_id`。Strike 的 `confirmed` 结论必须引用其自身 step 新采集的证据，且不能复用来源 session。工具调用 ID 只在 session/step 内有意义，在新 session 中可能重复。服务端在写 Fact 与更新 Finding 的同一事务里检查证据归属和工件 SHA256，失败时保持 Finding `pending` 且 step 可重试。`refuted` 与 `blocked` 可以没有请求证据，必须保留各自的语义。

这是一条可核对的来源与独立采集合同。AI 的 `confirmed` 仍是候选验证结果，提交比赛前需要人工复现并核对影响、授权范围与报告内容。
