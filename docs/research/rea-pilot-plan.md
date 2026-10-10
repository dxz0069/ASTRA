# REA 静态工件分析试点与评测方案

状态：**已完成五个冻结样本各三轮 REA 与 read/rg 基线实测**（2026-10-10）；逐项结果见 [rea-pilot-results.md](rea-pilot-results.md)。50 项正断言中 44 项有符合要求的结构证据，23 项边界全部保留；这些数字只评估离线 JS/Electron 工件输出，不能据此声称模型解题胜率、赛事得分或漏洞检出率提高。

## 决策与边界

- 上游：`morluto/rea`，本地 `E:\study\项目\research\rea-research`，提交 `c73cf0a`，`package.json` 版本 `6.3.0`，仓库 `LICENSE` 为 MIT。只参考接口和测试工件；当前不将 REA 源码、二进制、`dist/`、`node_modules/` 或其 provider 项目复制入 ASTRA。
- 入口：上游文档公开的 `rea analyze-javascript-application ABS_DIRECTORY --json`。运行前先证明现有入口与依赖完整；不把“文件存在”当成安装完成。本轮不执行目标应用、Electron renderer、bundle 或 native addon，也不访问网络目标。
- 本机 Node `22.23.2`、Java `17.0.16`、Python `3.10`；尚未确认 Ghidra 可用。因此首轮限 JS/Electron 静态 lane；原生反汇编、运行时观察、Android/固件均另设试点。
- ASTRA 的接入候选为**有来源位置的摘要**，提供给现有 Pi agent 作为线索。REA 输出必须保留 `observed`、`inferred`、`unknown`、`unavailable`、`partial`、`truncated` 等语义；推断关系不得升格为已运行或已验证。原始 JSON 仅作为本地评测工件，不直接塞进上下文。

## 冻结样本和判分点

所有样本来自 REA 仓库 `c73cf0a` 的公开测试工件/生成器。给每个任务保存输入清单及 SHA256；生成样本只调用 fixture 写入器，绝不加载生成后的目标脚本。

| ID | 输入 | 必须可核验的正断言 | 必须保留的负断言或边界 |
|---|---|---|---|
| J1 CLI | `tests/conformance/readiness/javascript-cli/` | `package.json` 的 `bin=index.js`；`index.js` 导入 `node:child_process`、`node:fs/promises`、`node:http`、`node:process`、`node:readline`；`spawn`、`readFile/writeFile`、`createServer` 可定位到源码 | 环境变量决定配置路径；`127.0.0.1` 和端口表达式只表示静态候选，不能声称已启动监听或知道运行端口 |
| E1 Electron IPC | `tests/conformance/readiness/electron/` | package main → `main.js`；BrowserWindow → `preload.js` → `renderer.html`；utility → `utility.js`；`readiness:echo` 的 `ipcMain.handle`/preload `invoke`/renderer `echo` 关系；`deep-link` 发送/订阅；字面 URL | 只可说唯一匹配的静态调用候选；不能称 IPC 已执行。未显式声明的 `nodeIntegration/contextIsolation/sandbox/webSecurity` 保持未知 |
| E2 歧义边界 | `tests/fixtures/electronBoundaryApplication.ts` 生成的目录 | 三个 BrowserWindow 的显式与动态选项；`rea:read`/`rea:write` 唯一配对；bridge `reaApi` 成员；动态 channel 与本地 native addon 路径 | 两个 `rea:ambiguous` handler 不得任选一个建立确定边；`rea:missing` 未配对；动态选项/频道未知；URL 检查只是候选；四字节 `.node` 文件不是真实 native export |
| A1 构建工件 | `tests/fixtures/javascriptArtifactApplication.ts` 生成的目录 | package main、preload、HTML → renderer；Webpack 模块 `1`,`2` 与 Rspack 模块 `src/editor.ts`,`src/model.ts`；路由 `/items/:id`；endpoint、storage、Worker/service worker、本地 source map 的 `src/renderer.ts` | 上游测试期望 `statistics.modules=4`、parse failures `0`。endpoint 字面字符串不等于已发送请求；伪 ELF header 不等于可分析 native 文件；静态分析不得运行含 throw 的 bundle |
| A2 损坏输入 | A1 的副本：`package.json`、`broken.js`、`renderer/renderer.js.map` 写入损坏文本 | 正常文件中仍可定位的事实应保留 | `statistics.parse_failures>0`；graph coverage `partial`、`truncated=false`、`omitted_count=null`；unknown operations 包含 `parse-javascript`、`parse-package-json`、`parse-local-source-map`；不能当成完整分析或无风险证明 |

E2 与 A1 的生成器分别导出 `writeElectronBoundaryFixture(root)`、`writeJavaScriptArtifactFixture(root)`。A2 依照上游 `tests/boundary/filesystem/javascriptArtifactReconstruction.test.ts` 第 63–92 行变造：`package.json`=`{`、`broken.js`=`function broken( {`、`renderer/renderer.js.map`=`{`。A1 的 endpoint 含公开 fixture 查询参数；查询值在送入 ASTRA 的摘要中应脱敏。

## 已冻结上游文件 SHA256

以下哈希为本地 `c73cf0a` 文件 SHA256；执行前逐项复核，不符即重冻结并解释原因。生成目录还需逐文件记录 SHA256。

| 文件（相对 REA 根） | SHA256 |
|---|---|
| `LICENSE` | `C1E889027B0D1D14F6B2C7FD3D2D7FECC22854A1C3E22C8772EC4A6D73121864` |
| `tests/conformance/readiness/javascript-cli/package.json` | `66B88B6C2E204544575A0C3C0A9314933BFA81E2D53305991B1D602DB9B6C81B` |
| `tests/conformance/readiness/javascript-cli/index.js` | `F6C6C9FC2D5B80E86113F4FAFAD4CD1B05C4FA9F72BC9650AF1878AD160577D8` |
| `tests/conformance/readiness/electron/package.json` | `519C45DE37EE712C467EAF7D3927E70B9FF999FA41446CD3E28EEFB958B1191F` |
| `tests/conformance/readiness/electron/main.js` | `F3BC2180B7A018B12070E994EF0982556BAA5759590F40F6775E558DD2379BB6` |
| `tests/conformance/readiness/electron/preload.js` | `48CB06677E1F438759689F988CADA5766536B67F8754DAA530C3B11DE2D583ED` |
| `tests/conformance/readiness/electron/renderer.js` | `8218243A5C3AACCAF70806B142F612763F082FA87A0FF31D174124BAD60EDDF5` |
| `tests/conformance/readiness/electron/renderer.html` | `11873DDEC003EB02FB7D4745095D995401A1699B5FCA96FFD67B33A86303F929` |
| `tests/conformance/readiness/electron/utility.js` | `1DD06DDE255BB8B0412C18F19DD981251ED6C8E62C2DC68B6A59A3985C0CAED3` |
| `tests/fixtures/javascriptArtifactApplication.ts` | `8EBA400224FB9A50EBC794B82CACF8D83BDD22E7136D316C0E5813BEB086BC88` |
| `tests/fixtures/electronBoundaryApplication.ts` | `11896021682545A7478453EF0EA8751395045441982C399C08500CB1421C7FE0` |

## 对照、运行和记录

1. **确定性基线**：固定 `rg -n` 查询和 `package.json`/源码文件清单，提取入口、导入、BrowserWindow、preload、IPC、URL、模块与 source map 候选及行号。文本命中仅能获“定位”分；跨文件关系须实际有可复核边。不得为了让 REA 显优而故意削弱已存在的 ASTRA 检索能力。
2. **REA**：先检查既有 CLI 可用性和 `--help`，再对每个冻结目录调用 `rea analyze-javascript-application <绝对目录> --json`；若使用源码仓库的 `node scripts/rea.mjs`，先确认构建产物、依赖和入口均齐全。不使用会自动下载安装的 `npx -y ...@latest`。每任务三轮，首轮单列并说明 OS 缓存未受控；后两轮比较确定性和波动。失败也保存退出码和输出。
3. **证据记录**：每轮保留 argv、上游 commit、输入文件哈希、退出码、墙钟时间、stdout/stderr 字节数与 SHA256、原始输出路径、可解析状态、coverage/status、错误与 unknown operations。标注从进程启动到完整输出关闭的耗时；CLI 只返回最终 JSON 时，不伪造“首个流式线索”时间。
4. **评分**：对每项正断言记 `correct / missing / wrong / unsupported`；负断言和未知状态记 `preserved / violated / unavailable`。`wrong` 或 `violated` 尤其是虚构 IPC 确定边、隐去 parse failure、把未观察行为说成已运行，视为硬失败。统计每任务正确覆盖率、错误断言数、运行失败数、三轮结果差异、p50/p95（样本量仅三次时同时列原始值）、原始 JSON 与摘要的字节/词元规模，以及基线差值。`unsupported` 不计成正确，也不把工具未覆盖的事实算作错误。
5. **验收**：所有安全和未知状态断言通过、没有错误结论，才允许接入候选摘要；是否启用由实测收益、时延和上下文成本决定。若任一 E2/A2 边界失败，先修摘要闸门或保留试点，不扩大到真实项目。之后在授权、可复现的 ASTRA 任务上单独做 agent A/B 测试，才可讨论解题收益。

原始输出、生成工件和计时日志保存在工作树的忽略目录 `tmp/rea-pilot/eval-20261010-c73cf0a/`，避免把公开测试项目、伪 native 文件或 REA 产物提交进 ASTRA。提交到 ASTRA 的内容限本方案、评测摘要和必要的可审核适配代码。

## 已完成的核验

- 使用现有源码入口 `node scripts/rea.mjs analyze-javascript-application ABS_DIRECTORY --json`，15 次均退出码 0，无超时，输出均可解析。没有重新安装 REA，也没有执行被分析的 fixture 应用。
- 38 个冻结输入文件的字节数和 SHA256 均与清单匹配；15 轮 stdout/stderr 的字节数和 SHA256 均与运行索引匹配。每个样本三轮 stdout 和评分一致。
- 固定查询、文件清单与完整行号源码读取组成 read/rg 基线，15 次均成功；不是仅靠弱化的正则查询比较。五个样本的完整源码定位均可提供，跨文件语义边和解析覆盖诊断仍需额外判断。

## 当前未验证事项

- 接入 ASTRA 的摘要缩减率、摘要是否忠实保留 `unknown/partial/inferred`、适配器异常路径仍由产品集成验收单独核验；原始输出不直接进入 Pi 上下文。
- 没有大体积真实构建工件、原生分析、动态 Electron、模型 A/B 或比赛环境测试。当前时延仅为本机工具进程时延，没有 agent 效率、准确率或得分提升的实证数字。
- REA 的六项结构缺口仍存在：CLI 的 `package.bin`，E1 的 `require.resolve` preload/utility、renderer→bridge 关系、deep-link 发送关系和 URL 字面证据。必要时由原有 read/rg 定位和人工/Pi 核对补充。
