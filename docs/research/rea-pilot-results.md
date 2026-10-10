# REA 静态工件分析试点：冻结样本实测

日期：2026-10-10。上游 [morluto/rea](https://github.com/morluto/rea) 固定在 `c73cf0a`，`package.json` 版本 `6.3.0`。本报告评估其公开 JS/Electron 测试工件对 ASTRA 的**静态线索价值**。没有执行被分析的应用、Electron renderer、bundle 或 native 文件；没有做模型 A/B、比赛环境评测或漏洞检出率测试。

## 口径与可复核材料

- 五个样本 J1、E1、E2、A1、A2 来自该版本 REA 仓库的 readiness/conformance 与 fixture 生成器。输入清单在本地忽略目录 `tmp/rea-pilot/eval-20261010-c73cf0a/manifest.json`；38 个输入文件字节数和 SHA256 均匹配。样本来自 REA 自己的测试集，可能偏向其支持的语法与结构。
- REA 使用既有入口 `node scripts/rea.mjs analyze-javascript-application ABS_DIRECTORY --json`。每个样本执行三轮；从进程启动到完整输出关闭计时。15 轮均退出码 0，无超时，可解析为 JSON。`run-index.json` 保存命令、输入、时间、输出字节数和 SHA256；15 轮 stdout/stderr 哈希均复核通过。每个样本三轮 stdout 逐字节一致。
- `score.mjs` 对既有 JSON 重评分，不重复运行分析；`results.json` 保存每一项源码真值位置、结构证据 JSON 指针、状态与各轮计时。正断言仅当所要求的结构节点/边存在才记 `correct`；仅有源代码字面值、其他较弱节点或人工可推断关系记 `missing`。边界项检查具体的 `unknown`、`partial`、歧义或非执行声明。`missing` 不表示源代码不存在，`preserved` 不保证没有其他未抽查的错误输出。
- 对照组固定 `rg --files`、`rg -n` 查询及所有文本源码的完整行号读取，也列出 `.node` 文件大小和有界头部。其运行、查询、输入哈希与判读见忽略目录 `baseline-read-rg/baseline-results.json`、`baseline-read-rg/baseline-score.md`。文本输出使这些小样本的源码位置均可人工核对，但不会自动给出跨文件关系或解析健康状态；因此没有把“人工能看到”算成“工具已断言”。两组都没有 LLM 调用。原始 REA JSON 和公开 fixture 中有形似令牌的 URL 参数，进入 agent 上下文前须截去查询值。

机读路径均相对于本工作树根目录。忽略目录仅供本机复核，不纳入项目交付；报告中的公开测试 URL 已省略参数值。

## 逐项评分

共 **50 项正断言，44 项有要求的结构证据，6 项缺失；23 项边界断言全部保留**。这是冻结断言覆盖数，不是准确率、召回率、模型胜率或比赛得分。`results.json` 的 `claims[].evidence` 给出每个命中在 REA `normalized_result` 中的确切 JSON 指针。源码位置以下均相对于对应样本目录。

| 样本 | 正断言 | 边界断言 | 结构证据缺口 | application graph 覆盖 |
|---|---:|---:|---|---|
| J1，CLI | 11/12 | 2/2 | `package.bin` | complete |
| E1，Electron readiness | 6/11 | 4/4 | preload 与 utility 的 `require.resolve`、renderer→bridge、deep-link send、外部 URL 字面值 | partial |
| E2，Electron 边界 | 10/10 | 6/6 | 本次断言未发现 | partial |
| A1，构建工件 | 14/14 | 4/4 | 本次断言未发现 | complete |
| A2，损坏输入 | 3/3 | 7/7 | 本次断言未发现 | partial |

表中的覆盖状态只来自 `normalized_result.graph.coverage.status`。五个样本的 `normalized_result.semantic_graph.coverage.status` 均为 `unknown`，其具体关系族均标有 `partial`；包括 J1/A1 在内，**不能将 application graph 的 `complete` 解释为整个语义分析完整**。适配器需要分别保存两层覆盖状态。

### J1：CLI

| 断言键 | 源码真值 | REA |
|---|---|---|
| `package_bin` | `package.json:5` 的 `bin=index.js` | **missing**：`package` 观察未暴露 `bin`。read/rg 可直接定位。 |
| `import_node:child_process`、`import_node:fs/promises`、`import_node:http`、`import_node:process`、`import_node:readline` | `index.js:1-5` | 5/5 `imports` 边，**correct**。 |
| `spawn`、`readFile`、`writeFile`、`createServer` | `index.js:21,10,31,22` | 4/4 语义节点，**correct**。 |
| `config_env_default`、`server_listen_site` | `index.js:8,25` | 2/2 静态候选，**correct**。 |
| `port_remains_dynamic`、`no_runtime_listening_claim` | `index.js:25` | 2/2 **preserved**：端口为配置表达式，结果没有声称运行时监听端口。 |

### E1：Electron readiness

| 断言键 | 源码真值 | REA |
|---|---|---|
| `package_main`、`window_renderer`、`html_script` | `package.json:4`、`main.js:25`、`renderer.html:6` | 3/3 **correct**。 |
| `preload_resolved`、`utility_resolved` | `main.js:22,28` | 2/2 **missing**：`require.resolve(...)` 被记录为动态表达式，preload 与 utility 未解析为对应文件。 |
| `echo_pair`、`bridge_members`、`deep_link_listener` | `preload.js:3-5`、`main.js:34` | 3/3 **correct**：唯一字面频道的静态配对候选、bridge 成员和监听端可定位。 |
| `renderer_echo_bridge_relation` | `renderer.js:3` 调 `window.readiness.echo` | **missing**：语义图有调用/属性读取位置，但没有所需的 renderer→bridge/频道边。 |
| `deep_link_send` | `main.js:12` 发送 `deep-link` | **missing**：有调用点，缺少带频道的发送结构关系。 |
| `external_url_literal` | `main.js:35` 的 `shell.openExternal` 常量 URL | **missing**：没有所需的结构化 URL endpoint 观察；源码文本可定位。 |
| `preload_stays_unresolved`、`utility_stays_unresolved`、`unspecified_preferences_unknown`、`ipc_is_static_pair_candidate` | `main.js:21-28`、`preload.js:4` | 4/4 **preserved**：未把未显式写的 Electron 选项当已知值，配对证据标为 `inferred`。 |

这组样本揭示一个实际取舍：REA 在同一输出中给出 IPC 候选与 bridge 线索，同时对 `require.resolve`、renderer 调用和 `webContents.send` 没有完成所需连接。摘要不能补造这些边；可附带源代码位置交给现有 `read/rg` 和 Pi 核对。

### E2：Electron 边界

| 断言键 | 源码真值 | REA |
|---|---|---|
| `three_windows`、`window_one_explicit`、`window_two_explicit`、`window_three_dynamic` | `main.js:6-23` | 4/4 **correct**，第三个窗口选项保持 dynamic。 |
| `rea_read_pair`、`rea_write_pair`、`bridge_members` | `main.js:25,29`、`preload.js:5-11`、`renderer/renderer.js:3-4` | 3/3 **correct**：字面频道配对与 `reaApi` 成员有结构证据。 |
| `dynamic_channel_recorded`、`native_addon_path` | `preload.js:17`、`main.js:4` | 2/2 **correct**：保留动态频道和 `.node` 请求路径。 |
| `utility_module_path` | `main.js:37` 的 `./utility/worker.js` | **correct**：记录的是字面 `module_path`，`module_resolution_context=module-specifier`；不解释为已在运行时加载。原评分脚本误要求去掉 `./`，已修正并对现存三轮输出重新评分。 |
| `ambiguous_not_paired`、`missing_not_paired`、`dynamic_not_paired` | `main.js:33-35`、`preload.js:15-17` | 3/3 **preserved**：两个同频道 handler 未被任意选一个配对，缺失和动态频道也没有伪造配对。 |
| `sender_check_only_candidate`、`native_export_not_verified`、`dynamic_window_stays_unknown` | `main.js:23,25-31`、`native/addon.node` | 3/3 **preserved**：URL/sender 检查只是候选；四字节 `.node` 文件不构成已验证导出。 |

### A1：构建工件

| 断言键 | 源码真值 | REA |
|---|---|---|
| `package_main`、`preload_resolved`、`html_script` | `package.json:4`、`main.js:5`、`renderer/index.html:1` | 3/3 **correct**。 |
| `module_1`、`module_2`、`module_src/editor.ts`、`module_src/model.ts` | `renderer/chunks/webpack.js:6,10`、`renderer/chunks/rspack.js:5,8` | 4/4 **correct**：静态识别四个模块，不执行 bundle。 |
| `route`、`network_endpoint`、`local_storage`、`indexed_db` | `renderer/renderer.js:4-7` | 4/4 **correct**；endpoint 是静态 `fetch` 调用候选。 |
| `worker`、`service_worker`、`source_map_original` | `renderer/renderer.js:8-9`、`renderer/renderer.js.map:1` | 3/3 **correct**：本地 map 含 `src/renderer.ts` 的来源引用。 |
| `modules_count_four`、`parse_failures_zero`、`network_is_static`、`native_not_analyzed` | 上述源码与 `native/addon.node` | 4/4 **preserved**：四模块、零解析失败，未把 URL 当网络行为或伪 ELF 头当可验证 native 导出。 |

### A2：损坏输入

| 断言键 | 源码真值 | REA |
|---|---|---|
| `normal_route_survives`、`normal_worker_survives`、`normal_modules_survive` | 未损坏的 `renderer/renderer.js:4,8`、`renderer/chunks/webpack.js:6` 等 | 3/3 **correct**：坏文件没有吞掉可解析文件的这些结构事实。 |
| `parse_failures_positive`、`coverage_partial`、`not_truncated` | `broken.js:1`、`package.json:1`、`renderer/renderer.js.map:1` | 3/3 **preserved**：解析失败数大于零，图 `partial`、`truncated=false`、`omitted_count=null`。 |
| `unknown_parse-javascript`、`unknown_parse-package-json`、`unknown_parse-local-source-map` | 同上三处损坏输入 | 3/3 **preserved**：三种 unknown operation 逐项存在。 |
| `bad_package_not_accepted` | `package.json:1` 只有 `{` | **preserved**：未将坏 package 当成有效入口。 |

## 时间与输出规模

两组都测完整工具输出，均不含 Pi 推理或人工核对。REA 是单次 Node CLI；基线是文件清单、`rg` 与完整文本读取的连续操作。样本每组只有三轮，表中 p95 按 nearest-rank 等于三次最大值，不能当作稳定的总体尾延迟。首轮标明但 OS 缓存未受控。

| 样本 | REA 三轮 ms | REA p50 / p95 ms | REA JSON 字节/轮 | read/rg 三轮 ms | read/rg p50 / p95 ms | read/rg 文本字节/轮 |
|---|---:|---:|---:|---:|---:|---:|
| J1 | 1325.744 / 1253.035 / 1279.981 | 1279.981 / 1325.744 | 300,429 | 70.179 / 29.956 / 24.433 | 29.956 / 70.179 | 2,550 |
| E1 | 1323.824 / 1344.374 / 1364.018 | 1344.374 / 1364.018 | 540,697 | 75.249 / 22.965 / 28.131 | 28.131 / 75.249 | 4,040 |
| E2 | 1379.333 / 1351.252 / 1358.972 | 1358.972 / 1379.333 | 596,743 | 79.449 / 44.477 / 31.056 | 44.477 / 79.449 | 5,884 |
| A1 | 1367.668 / 1415.928 / 1375.581 | 1375.581 / 1415.928 | 410,316 | 82.917 / 24.482 / 32.098 | 32.098 / 82.917 | 5,484 |
| A2 | 1390.299 / 1389.268 / 1389.683 | 1389.683 / 1390.299 | 409,773 | 73.016 / 28.782 / 22.903 | 28.782 / 73.016 | 4,775 |

在这些很小的文件上，read/rg 更快且原始输出小得多；REA 的价值是自动关系候选与解析/未知状态。原始 REA JSON 为 300–597 KB，每轮远超基线的 2.5–5.9 KB，不适合直接注入 Pi 上下文。两组内容密度和功能不同，字节与时延差不能被解释成 agent 总任务性能差。适配器若要启用，应按预算输出少量带来源的观察，保留 `observed/inferred/unknown/partial`，并让调用者按路径再读源码。

## 接入判断与下一步验证

本轮支持把 REA 作为**可选、离线的 JS/Electron 静态线索工具**试用；默认检索仍要能直接查原文。A1 的 bundle 模块、source map、worker 和 E2 的 IPC 歧义/损坏覆盖状态有可用结构证据；E1 的五项缺口须在摘要中显式标出未知或缺失，不可由适配器补全。对于 `.node` 与 sender 检查，只能报告请求路径和检查候选，不能声称 native export 已验证或授权已成立。

## ASTRA 适配器端到端试用

在同一台 Windows 主机上，使用本仓库的 `astra static-js`、独立安装的 REA 入口和冻结的 J1/E1/E2 输入，实际走通目录快照、REA 子进程、候选摘要、证据保存和按文件的 semantic 分页查看。J1 本次进程耗时 1,530 ms，REA 原始 JSON 为 300,333 字节；application 覆盖 `complete`，semantic 覆盖 `unknown`。E1 本次进程耗时 2,235 ms，原始 JSON 为 540,601 字节，摘要为 7,980 字节；application 覆盖 `partial`，semantic 覆盖 `unknown`，`main.js` 的 semantic 结果统计为 179 个节点、125 条关系和 66 个 unknown。E2 本次进程耗时 1,782 ms，原始 JSON 为 596,647 字节，ASTRA 摘要为 8,052 字节；application 覆盖 `partial`，semantic 覆盖 `unknown`。E2 的 `main.js` semantic 首页输出约 9,965 字节，因 12 KiB 输出预算将请求的 10 条缩至 7 条并返回 `next_offset=7`，继续翻页不会跳过条目。以上耗时都是单次本机进程测量，不构成稳定性能结论。

Windows 临时目录可能包含 8.3 短路径；REA 输出会改成规范长路径。适配器现以目录身份核验活动快照，在保存后的视图中核对 manifest 记录和证据 SHA256。适配器测试覆盖关闭开关、输入和输出预算、畸形 envelope、敏感值有限脱敏、工作区归属及证据篡改拒绝。此处的散列用于检测本地意外变化；对能同时修改 manifest 和证据的同权限进程不提供防篡改认证。输入限定为可信、分析期间静止的本地目录，Windows 路径复查和进程资源限制不是操作系统沙箱。

仍需在有授权且可复现的 ASTRA 任务上独立做 agent A/B，记录定位时间、有效发现和误报，之后才能讨论对赛事任务的实际收益。当前工具试用不能推出模型解题率或比赛成绩提升。
