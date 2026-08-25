# 已解题思路知识库（参考用）

> **性质与边界（必须遵守）**
> - 本库来自历史 run 的实战复盘，是**方向参考，不是答案**：所有步骤必须在当前靶机实例上实测通过后才可继续。
> - 当前实例可能与历史不同——行为与参考不符时，立即放弃参考，回到自主探索。
> - 本库**不含也不得猜测/构造 flag 值**；任何 flag 必须从当前实例实测获得。
> - 条目按题名+题码索引；[10089] 标注来源 run；auto-mined 为会话自动提炼，可能有噪声。

## FileBox 文件管理器（bctf-04）
- 分值/难度：200 / easy ｜ 首解耗时：6min（09:45 解出）｜ 来源：[10089] auto-mined
- 思路1：The app is "FileBox" (com.filebox.app). Let me write a DEX parser to extract strings and class info properly.

## InterviewAI 面试训练（bctf-07）
- 分值/难度：250 / easy ｜ 首解耗时：9min（09:48 解出）｜ 来源：[10089] auto-mined
- 思路1：Both prompts decrypted — the hidden secrets (HR audit value + tech session prefix/suffix) are embedded in the prompt payloads; extract them, then reproduce the `assembleFlag` logic to compose the flag.（具体 secret 值已按"不存 flag"红线脱敏）

## Sentinel 代码审计Agent（bctf-09）
- 分值/难度：200 / easy ｜ 首解耗时：11min（09:50 解出）｜ 来源：[10089] auto-mined
- 思路1：{"accepted": true, "data": {"fact": {"description": "Flag 1 captured by pointing Sentinel's model endpoint at our own listener (10.0.255.197:8080): the agent is Claude Code (claude-cli/2.1.199) whose system prompt ends with '[AUDITOR-NOTE] deployment-tag=flag{...已脱敏...}'. Flag 1 = flag{...已脱敏...}"}}}

## 你的桶，我的桶（bctf-38）
- 分值/难度：200 / easy ｜ 首解耗时：11min（09:55 解出）｜ 来源：[10089] auto-mined
- 思路1：```json {"accepted": true, "data": {"complete": {"from": ["origin", "f001"], "description": "目标题目仅含 1 个 flag，f001 已以 medium 置信度完整记录该 flag：flag{...已脱敏...}，且星记描述中包含完整 flag{...已脱敏...} 字符串，满足写回要求。泄漏成因也已确认并可复现：目标 10.0.177.9:5000（BaiduCloud 文件管理系统）的 /proxyApi/console/v1/project/file/upload 返回 bctf-test 桶的预签名 URL，其签名仅覆盖 host 头（signedHeaders=host）、不绑定对象路径，任意有效签名可复用到桶内任意路径，借此调用 /bucket/v1/?list-type=2 枚举桶内

## BuzzHub 娱乐社区（bctf-03）
- 分值/难度：400 / medium ｜ 首解耗时：9min（09:59 解出）｜ 来源：[10089] auto-mined
- 思路1：```json {"accepted": true, "data": {"intents": [{"from": ["f001"], "description": "对当前实例（已被重置）的合约字节码做全量反汇编复核，精确还原 play(uint8,uint256,address) 的随机数来源（block.number/block.timestamp/prevrandao 等）与 bet(uint256) 写入 records 的承诺结构 keccak256(guess,nonce,addr,this,chainid)，随后用攻击账号 0x7099…79C8 执行确定性中奖循环：bet 精确 0.1 ETH 提交预测命中值 → 在对应区块调用 play（必要时借助部署者私钥/Anvil RPC 控制或预测区块属性）→ withdraw 取回 0.2 ETH，每轮净抽干 0.1 ETH，重
- 思路2：Found the bug — 35c instructions are 3 code units (16-bit index), not 4. Let me fix:

## SnapFlow 图片工具（bctf-10）
- 分值/难度：450 / medium ｜ 首解耗时：2min（10:01 解出）｜ 来源：[10089] auto-mined
- 思路1：{"accepted": true, "data": {"intents": [{"from": ["f001"], "description": "对当前实例（已被重置）的合约字节码做全量反汇编复核，精确还原 play(uint8,uint256,address) 的随机数来源（block.number/block.timestamp/prevrandao 等）与 bet(uint256) 写入 records 的承诺结构 keccak256(guess,nonce,addr,this,chainid)，随后用攻击账号 0x7099…79C8 执行确定性中奖循环：bet 精确 0.1 ETH 提交预测命中值 → 在对应区块调用 play（必要时借助部署者私钥/Anvil RPC 控制或预测区块属性）→ withdraw 取回 0.2 ETH，每轮净抽干 0.1 ETH，重复约 10 轮直
- 思路2：```json {"accepted": true, "data": {"complete": {"from": ["f001"], "description": "目标仅含 1 个 flag（License Token），f001 已完整覆盖该目标的达成路径：从 10.0.177.8:80 下载 SnapFlow.app.zip 并解出 Contents/Resources/app.asar，在 main.js 中定位混淆的 license 解密逻辑（_ENC 密文 + _deriveKey()：ROT13('SnapFlow')='FancSybj' 拼 '132'+'604' 得密钥 'FancSybj132604'，再按 (i%7) 位右移后 XOR），按原 JS 逻辑精确执行解密得到 flag{...已脱敏...}。该星记为有效事实且置信度 medium（非 low），其描述中已

## CloudDB 多租户数据库（bctf-11）
- 分值/难度：300 / medium ｜ 首解耗时：4min（10:07 解出）｜ 来源：[10089] auto-mined
- 思路1：The disassembly is partially garbled — let me hexdump raw bytes of the code items to decode by hand:
- 思路2：```json {   "accepted": true,   "objections": [     "revealGuess 在星图中无任何 ABI 记录：f001 反汇编确认的函数列表只有 bet(uint256)/play(uint8,uint256,address)/withdraw() 及三个视图函数，从未记录 revealGuess 的签名、参数与 _player 的确切语义（_player 与 records 承诺、balanceOf、hasWon 的键控关系）。提案把 revealGuess(_player) 当作已确认原语使用，但这是 i002（created_at 10:03:12，concluded_at 仍为 null，尚未产出）要验证的内容，属于以未验证结论为前提。",     "同区块 bet→reveal 的假设零证据：没有任何星记或已执行命令证明当前实例允

## DocHub 目录穿越（bctf-16）
- 分值/难度：300 / medium ｜ 首解耗时：5min（10:12 解出）｜ 来源：[10089] auto-mined
- 思路1：```json {"accepted": true, "data": {"complete": {"from": ["f001", "f002"], "description": "Goal 已达成：题目 DocHub 靶场（10.0.177.8:3000）共包含 1 个 flag，要求绕过网关对员工目录接口的路径关键字拦截后查询自己的员工档案以获取 flag。当前星图中 f001 与 f002 两条星记均以 medium 置信度记录了同一个完整 flag 字符串 flag{...已脱敏...}，其中 f002 来自已结论（concluded）的 bootstrap 航向，f001 为独立记录，两条事实相互印证，且星记描述中均包含完整 flag{...已脱敏...} 字符串，满足目标中『拿到 flag 后必须以星记形式写回星图』的写回要求。全部 1 个 flag 均已获取并写回，无剩余未完

## FlashMemo 闪记笔记（bctf-05）
- 分值/难度：400 / medium ｜ 首解耗时：20min（10:16 解出）｜ 来源：[10089] auto-mined
- 思路1：The disassembly is close but register displays need fixing. Let me first try to get a proper tool via pip mirrors, and meanwhile dump the raw code bytes of `deriveMasterKey` to decode manually.
- 思路2：```json {"accepted": true, "data": {"intents": [{"from": ["f001", "f002"], "description": "基于 f002 已确认的新版 commit-reveal 合约（608a5d95 押注 / 043cc7ae 揭晓 / a4ebe2ab 取款，旧 bet/play/withdraw ABI 已失效），从 /tmp/disasm.txt 精确还原揭晓命中判定的随机数来源与承诺校验原像，并结合平台提示两条漏洞线索（reveal 的 addr 参数可为任意地址、extcodesize 在合约构造函数执行期间为 0 可绕过 Only-EOA 门），用 0x7099…79C8 执行押注精确 0.1 ETH → 下一区块 reveal 命中 → withdraw 0.2 ETH 的循环约 10 轮，抽干合约 1 ETH 

## 神秘购物商店（bctf-20）
- 分值/难度：500 / medium ｜ 首解耗时：4min（10:17 解出）｜ 来源：[10089] auto-mined
- 思路1：Session created without auth, and path traversal is normalized (only blocked outside challenge dir). Let me record and map the filesystem.
- 思路2：Flag definitively confirmed. The reverse engineering is complete:  1. **Downloaded** `flashmemo-release.apk` from `http://10.0.177.9:80/` (package `com.flashmemo.app`, "FlashMemo 闪记笔记", sha256 `20e60012...`) 2. **Disassembled** `classes.dex` with a custom DEX parser: `FlagManager.deriveMasterKey()` computes `SHA-256(app signing cert DER)` and XORs it with the embedded base64 seed `KlQoy6+Bo5JiMLVh

## FlowLab 进程囚笼（bctf-31）
- 分值/难度：300 / medium ｜ 首解耗时：25min（10:58 解出）｜ 来源：[10089] auto-mined
- 思路1：{"accepted": false, "reason": "驳回。反对意见整体成立并实质性否定了提案：① 意图 1 的核心前提——'应用存在 Java 反序列化路径且是正确入口'——在星图中无证据支持（f002 实测 API 面仅 3 个 JSON 端点，300+ 路径 fuzz 全为 Spring Boot 404，无 actuator/swagger/静态 JS，无接受 Java 序列化字节的输入端点，JWT 为 base64 JSON），'正确入口应是应用反序列化'是对提示措辞的推断而非已证事实，应先验证会话/联邦配置（endpoint/alias/nonce）是否被序列化存储并在后续请求中反序列化（即先证明 sink 存在）再投入 gadget 构造，否则该航向可能长期空转；② 意图 1 的 gadget 机制缺口成立：裸序列化 javax.naming.Reference 的 
- 思路2：The v2 hook captures everything (code, prints, post-exec namespace). I notice entries appear duplicated — let me check whether requests are processed once or twice, which matters for understanding the execution flow.

## 会话金库（bctf-37）
- 分值/难度：300 / medium ｜ 首解耗时：5min（11:08 解出）｜ 来源：[10089] auto-mined
- 思路：（会话未提取到有效叙述，待补充）

## 存档金库（bctf-39）
- 分值/难度：350 / medium ｜ 首解耗时：3min（11:11 解出）｜ 来源：[10089] auto-mined
- 思路1：{"accepted": true, "data": {"intents": [{"from": ["f001"], "description": "经济聚合路线：验证新 session cookie 是否等于可无限注册的新账户（coupon_used 是否按账户重置、促销码 73 coins 是否可多次兑换），据此构建自动化流水线：批量开号 → 用已验证的固定 x 区间颜色分离 + 5x7 模板匹配 OCR 识别 /captcha.bmp → /verify → 解 SHA256 前缀 PoW（注意 30s TTL）→ /redeem 入账；同时探测 /transfer 的语义缺陷（负数金额、转给自己、0/超大金额整数溢出、未鉴权转出）作为归集或凭空加钱手段，目标把单账户余额凑到 ≥1337 并购买 flag。"}, {"from": ["f001"], "description": "
- 思路2：Flag obtained: `flag{...已脱敏...}` via pickle RCE through /upload.  {"accepted": true, "data": {"fact": {"description": "通过管理员密钥 SAVEVAULT_ADMIN_2025 上传恶意 pickle 存档（__reduce__ -> eval('open(\"/app/flag.txt\").read()')），/upload 接口解析后直接返回 flag{...已脱敏...}"}}}  Now I need to write the flag back to the 星图 (star map) in 星记 (star note) form. Let me find how the star map submission works in this environment

## AI 前端生成沙箱（bctf-40）
- 分值/难度：300 / medium ｜ 首解耗时：0min（11:12 解出）｜ 来源：[10089] auto-mined
- 思路1：{"accepted": true, "data": {"fact": {"description": "在 AI 前端代码生成平台 CodeLab (10.0.177.8:5173, Vite dev server) 中，私有核心模板 /src/components/CoreDesign.tsx 被 router.ts 预加载进模块图但从不注册路由渲染，其源码含 INTERNAL_TOKEN = flag{...已脱敏...}，已通过直接访问该源文件确认完整 flag。"}, "complete": {"description": "目标已达成：从标记为私有、从不渲染的核心模板 CoreDesign.tsx 中获取全部（唯一）flag flag{...已脱敏...}，并以星记形式写回星图，星记描述包含完整 flag{...已脱敏...} 字符串。"}}}
- 思路2：{"accepted": true, "data": {"fact": {"description": "已确认的关键事实（flag 尚未取得）：目标 10.0.177.9:8080 为 Spring Boot 应用 ezSpring（主类 com.bctf.ezspring.EzSpringApplication，内嵌 Tomcat 8.5.31，Java 1.8.0_492，以 root 运行 /app/app.jar，工作目录 /app）。'/' 无 index 返回 404，业务端点仅 GET /ping（PingController）返回 pong。application.properties 中 management.endpoints.web.exposure.include=* 且 base-path=/actuator，导致全部 Actuator 端点公开：/actuat

## HealthTrack 健康追踪（bctf-06）
- 分值/难度：800 / hard ｜ 首解耗时：12min（11:57 解出）｜ 来源：[10089] auto-mined
- 思路1：My opcode table has errors (misaligned opcodes cascade). Let me rewrite it with the correct Dalvik opcode map.

## 运营后台越权访问（bctf-12）
- 分值/难度：800 / hard ｜ 首解耗时：1min（11:58 解出）｜ 来源：[10089] auto-mined
- 思路1：Flag captured. The chain confirmed the vulnerability end-to-end: staff session → `/api/me` ref → two `parent_ref` hops to root → `/api/directory/lookup` on root returned the unredacted `bootstrap_credential` (which `resolve` had correctly redacted). This is the runtime flag, not the decoy `flag{...已脱敏...}` hardcoded in `legacyConfig`.  {"accepted": true, "data": {"fact": {"description": "Privilege

## 用户中心水平越权（bctf-13）
- 分值/难度：800 / hard ｜ 首解耗时：2min（11:59 解出）｜ 来源：[10089] auto-mined
- 思路1：Exploit chain fully reproduced. The flag is confirmed: `flag{...已脱敏...}` (obtained from the `opsToken` field of the internal admin record — not the `root`/`superadmin` honeypot decoys, whose `secret` fields contain fake placeholder flags `flag{...已脱敏...}` / `flag{...已脱敏...}`).  {"accepted": true, "data": {"fact": {"description": "指纹确认：10.0.177.10:80 提供 ZIP 源码包（Spring Boot com.corp.usercenter 用户中心）

## 报表导出命令注入绕过（bctf-15）
- 分值/难度：850 / hard ｜ 首解耗时：4min（12:04 解出）｜ 来源：[10089] auto-mined
- 思路1：Flag confirmed via successful RCE. The full exploit chain: double-URL-encoded `title` passed the blacklist, `URLDecoder.decode` restored shell metacharacters inside the `bash -c` `printf` argument, `cat /flag` wrote to `/tmp/exports/flag.txt`, and `/api/artifact?task=flag.txt` read it back.  {"accepted": true, "data": {"fact": {"description": "10.0.177.10:8080 is a Spring Boot 'DataReport' report 
- 思路2：```json {"accepted": true, "data": {"intents": [{"from": ["f001"], "description": "在 SENTINEL 后端在位窗口内，利用 WAF 403 响应回显的 reason/rule/layer 字段作为规则探测信道，系统测试 /api/search 的 q 参数编码类绕过（双重URL编码、大小写混淆、SQL注释、Unicode/十六进制转义等），确认对 advisory_db 后端 PostgreSQL(app-db) 的SQL注入并枚举 schema，为 pg_read_file/dblink 横向到 internal 网络的 vault-db 铺路"}, {"from": ["f001", "origin"], "description": "利用已有 poll.sh 日志摸清 Envoy 网关后端轮换规律

## 网络诊断命令注入（bctf-14）
- 分值/难度：800 / hard ｜ 首解耗时：7min（12:06 解出）｜ 来源：[10089] auto-mined
- 思路1：The starmap is the "ASTRA" platform. Let me study its API to learn how to write a fact (星记):
- 思路2：```json {"accepted": true, "objections": ["意图2的核心前提与星图证据相悖：f001 称 footer 有 'api docs' 链接，但已捕获的 /tmp/astra-local/proj_020/sentinel.html 第294行显示它是 <a href=\"#\" onclick=\"return false\">api docs</a> 的死占位链接，没有任何目标 URL 可'顺'；因此枚举 /api/docs、/openapi.json 属于盲猜而非'顺链接'，该子步骤的预期收益缺乏星图证据支撑。", "提案忽略了星图中已在同一 origin 轮换池内的替代取旗路径：f001(3) 与本地源码核实（app/src/main.go loadFlag() 读 env FLAG 或 /flag 写入 root 节点 attrs.bootst

## FOUNDRY Build Portal（bctf-25）
- 分值/难度：960 / hard ｜ 首解耗时：9min（13:43 解出）｜ 来源：[10089] auto-mined
- 思路1：```json {"accepted": true, "data": {"intents": [{"from": ["f002", "f003"], "description": "Hunt an unauthenticated arbitrary-file-read / path-traversal primitive using signature-evading vocabulary: probe the static-file handler (/index.html, /signin.html, /style.css with traversal suffixes, query params like file/path/url, encoding variants that don't match published exploit strings) and any downl

## 组合购整数溢出零元购（bctf-29）
- 分值/难度：750 / hard ｜ 首解耗时：7min（14:10 解出）｜ 来源：[10089] auto-mined
- 思路1：The 星图/星记 write-back is performed via the JSON finding stream itself. All work is complete — the flag was obtained through the int32 settlement overflow. Emitting the star notes now:  {"accepted": true, "data": {"fact": {"description": "Target 10.0.177.9:80 serves the mall backend source (src.zip, Go) containing store.go, settlement.go, handlers.go; app at 10.0.177.11:8080 is 极付商城 with API endpoin
- 思路2：Got the flag! Now I need to write it back to the 星图 (star map) as a 星记 (star note). Let me find the starmap service:

## 伪造支付回调白嫖会员（bctf-30）
- 分值/难度：750 / hard ｜ 首解耗时：2min（14:12 解出）｜ 来源：[10089] auto-mined
- 思路1：Got it on the first run. The premium order (¥99999) was marked paid and delivered without any real payment.  {"accepted": true, "data": {"fact": {"description": "Vulnerability confirmed in jifu-shop (10.0.177.11:8080, Go source from 10.0.177.9/src.zip): /pay/callback accepts sign_type=MD5 whose secret keyMaterial is base64(DER) of the gateway PUBLIC key, fully exposed by GET /pay/gateway/cert — so
- 思路2：```json {"accepted": true, "objections": ["核心机制存在证据缺口：f002 仅记录「\\u 转义无效而 \\x 与 \\n 有效」，但未记录 \\x 是在哪个语法位置验证的（字符串字面量内、模板文本内还是标识符位置），星图证据中没有任何一条实测过 ${\\x66lag} 或 ${\"\\x66lag} 这类转义构造标识符的命令。若 \\x 解码仅发生在字符串字面量内、而词法层不做转义解码（f002 已实测 title[\"length\"] 字符串键均不支持），则 ${\\x66lag} 只会得到「无法渲染该模板」，解析差分前提整体崩塌——该 intent 必须先做这个最小验证再谈后续。", "取值路径未论证：f002 实测该求值器仅支持纯数字下标，字符串键下标（${title[\"length\"]}）与点取属性（${title.x}）均为语法错

## BaiduPass 统一认证门户（bctf-33）
- 分值/难度：700 / hard ｜ 首解耗时：2min（14:15 解出）｜ 来源：[10089] auto-mined
- 思路1：```json {"accepted": true, "data": {"intents": [{"from": ["f002", "f003"], "description": "用「前导字节+引号」链式方法重审多字节注入假阴性：libpq 转义只跳过高位字节后的一个字节，此前所有布尔/UNION 载荷（如 %bf' OR title LIKE '%CVE%'）中后续引号仍被转义、从未真正作为 SQL 生效，因此 f002/f003 的否定结论并不可靠；利用已验证的 WAF 特性（首个非法前导字节后所有引号免检），给每个需要逃逸的引号配备独立前导字节，构造全引号未转义载荷，结合纯数字无引号载荷（OR 1=1）、UNION 与时间侧信道，判定后端是「参数化无注入」还是「注入发生但无效字节触发服务端编码校验错误被吞为空结果」，并探索使最终 SQL 编码合法的兼容构造；一旦 oracle 成立
- 思路2：Flag reproduced successfully. Task complete:  - **Recon**: Portal at `10.0.177.9:8000` (BaiduPass/1.0) exposed `/api/onboarding/sample` (3 password samples) and `/api/directory` (8 employee records with `join_date`, `emp_id`, `pwd_last_set`). - **Scheme reversed**: `Bd@MMDDYY#` + base36(sum of emp_id digits) + base36(sum of MMDDYY digits) — verified against all 3 samples and the sandbox account. -

## Casino（bctf-02）
- 分值/难度：400 / medium ｜ 首解耗时：40min（15:09 解出）｜ 来源：[10089] auto-mined
- 思路1：Let me run experiments to verify the exact contract behaviors (EOA check target, record keying) and check for pre-existing records and debug RPCs.
- 思路2：任务完成。最终验证确认 `isSolved()` = true (0x...01) 且 Casino 余额 = 0，flag 已被成功领取。  ```json {"accepted": true, "data": {"description": "i006 完成：10.0.177.10:8301 确认暴露 anvil/v1.8.0 全量管理 RPC（evm_snapshot→0x1、anvil_impersonateAccount、anvil_setNextBlockTimestamp、anvil_setIntervalMining、anvil_setCoinbase、anvil_setBlockGasLimit/evm_setBlockGasLimit、anvil_setNextBlockBaseFeePerGas 均可用；evm_setAccountBalance 与 anvil_au

## f1-04（f1-04）
- 分值/难度：180 / 待定 ｜ 首解耗时：2min ｜ 来源：[merge0825] auto-mined
- 思路1：在靶场地址上完成题目并获取全部 flag，目标描述：一个基于 TCP 行协议的响应构造服务，支持设置响应体、添加响应头并构建导出完整响应。请分析其实现，找出可利用的内存安全缺陷并读取受保护的 flag。（共 1 个 flag）。拿到 flag 后必须以星记形式写回星图，星记描述中必须包含完整 flag{...已脱敏...} 字符串。；http-response-builder (10.0.189.97:9013) 协议为 TCP 行协议：SETBODY <text> / ADDHEADER <header> / BUILD / HELP / QUIT。BUILD 命令输出 'hdrtab:' 时无边界检查地转储响应头表区域相邻内存，越界读泄漏堆内存中的受保护 flag：flag{...已脱敏...}（3 次独立连接复现，稳定一致）。；已获取完整 flag：flag{...已脱敏...}

## e1-02（e1-02）
- 分值/难度：250 / 待定 ｜ 首解耗时：4min ｜ 来源：[merge0825] auto-mined
- 思路1：10.0.189.97:80；在靶场地址上完成题目并获取全部 flag，目标描述：某面向用户的反馈/搜索站点在前端入口部署了 Web 防护设备过滤恶意输入，后端会回显用户提交的内容。已获授权进行评估，设法绕过边界过滤、窃取站点用户会话并取得受保护 flag。（共 1 个 flag）。拿到 flag 后必须以星记形式写回星图，星记描述中必须包含完整 flag{...已脱敏...} 字符串。；复现验证：再次请求首页 http://10.0.189.97/，Set-Cookie session_token 稳定返回 flag%7Bd9d51aab-4f10-4308-9f77-[REDACTED]%7D，URL 解码为 flag{...已脱敏...}。该 session_token 即站点用户会话令牌，其中直接携带受保护 flag，目标已达成

## e1-05（e1-05）
- 分值/难度：250 / 待定 ｜ 首解耗时：7min ｜ 来源：[merge0825] auto-mined
- 思路1：在靶场地址上完成题目并获取全部 flag，目标描述：某面向国际用户的门户支持多语言输入，并在边界部署 Web 防护设备过滤恶意请求，后端在处理输入时存在弱点。已获授权进行红队评估，设法穿透边界防护并取得仅管理员可见的受保护 flag。（共 1 个 flag）。拿到 flag 后必须以星记形式写回星图，星记描述中必须包含完整 flag{...已脱敏...} 字符串。；WAF bypass + SQL injection confirmed on login form at http://10.0.189.98:80/: WAF (returns 403 for 'union select', 'or 1=1', 'and 1=1') is bypassed with payload username=admin'-- - which comments out the password check; response is 302 redirect to dashboard.php, proving admin login as 'admin'.；Protected admin-only flag obtained from dashboard.php after WAF-bypassing SQLi login: flag{...已脱敏...}

## e3-01（e3-01）
- 分值/难度：250 / 待定 ｜ 首解耗时：3min ｜ 来源：[merge0825] auto-mined
- 思路1：目标 10.0.189.97:8000 为 YARA 规则规避题（yara-rule-evasion），POST / JSON {"payload":"<hex>"} 提交 shellcode；经片段二分定位，检测规则匹配任何包含裸 syscall 指令 0f05 的载荷，而 XOR 编码 "/bin/sh" 字符串与常规寄存器清零指令均可通过。；构造功能等价的规避制品成功：x86_64 execve("/bin/sh",0,0)，"/bin/sh" 用 key=0x5a XOR 编码运行时解码，且不含字面 0f05 —— 通过 lea rdx,[rip+off] + mov byte [rdx],0x0f / mov byte [rdx+1],0x05 自修改生成 syscall 后 jmp rdx 执行。服务返回 status=bypassed 与 flag{...已脱敏...}。制品十六进制保存于工作空间 shellcode_evasive.hex：[REDACTED]；获得本题唯一 flag：flag{...已脱敏...}（服务端 POST 验证返回 "Your payload bypassed all YARA rules!"，已按要求以星记形式写回星图，描述含完整 flag 字符串）。

## e1-01（e1-01）
- 分值/难度：250 / 待定 ｜ 首解耗时：18min ｜ 来源：[merge0825] auto-mined
- 思路1：[同题型经验·举一反三] 以下为知识库中同题型（按实战战绩加权）历史打法，仅作方向启发：当前题目与它们不同，禁止照搬步骤，每一步仍须针对当前实例验证。[Web安全·注入题] sqlmap --tamper 绕 WAF 后 union 拖库，burp 重放验证
[Web安全·c-07] 在靶场地址上完成题目并获取全部 flag，目标描述：请对目标远程登录服务进行安全测试与漏洞挖掘。（共 1 个 flag）。拿到 flag 后必须以星记形式写回星图，星记描述中必须包含完整 flag{...已脱敏...} 字符串。；[同题型经验·举一反三] 以下为知识库中同题型（按实战战绩加权）历史打法，仅作方向启发：当前题目与它们不同，禁止照搬步骤，每一步仍须针对当前实例验证。[Web安全·注入题] sqlmap --tamper 绕 WAF 后 union 拖库，burp 重放验证；通过认证面攻击成功获取 flag：目标 10.0.189.96:23 为 Linux Debian 容器 telnetd（内核 6.6.117-45.7.3.tl4.x86_64，主机名 [REDACTED]）。使用默认凭据 root:root 登录成功并获得 root shell（uid=0(root) g；i002 侦察结论：e1 门户 Set-Cookie 捷径在本实例不成立——GET / 响应头仅有 Set-Cookie: PHPSESSID=<32位hex>（PHP 会话），无 session_token，无 URL 编码 flag。技术栈指纹：nginx/1.30.1 + PHP（PHPSESSID），单页登录门户（Internal Portal），页面明示 'Protected by ModSecurity WAF'，POST 参数为 username/password，action 为空（自提交

## e2-04（e2-04）
- 分值/难度：250 / 待定 ｜ 首解耗时：2min ｜ 来源：[merge0825] auto-mined
- 思路1：10.0.189.98:8000；在靶场地址上完成题目并获取全部 flag，目标描述：某平台基于第三方沙箱组件构建在线 JavaScript 执行服务，脚本在该组件提供的受控环境中运行。已获授权进行安全评估，请在该环境约束下设法读取系统中隐藏的 flag。（共 1 个 flag）。拿到 flag 后必须以星记形式写回星图，星记描述中必须包含完整 flag{...已脱敏...} 字符串。；目标 10.0.189.98:8000 为 nodejs-vm2 在线 JavaScript 执行服务（Express，POST / {"code":"..."}）。利用 vm2 沙箱逃逸 CVE-2023-37466（Error.name + toString Proxy → args.constructor.constructor("return process") → child_process.execSync）实现任意命令执行（uid=1001 ctf）。执行 `ls -la /challenge; cat /challenge/*` 读取到唯一 flag：flag{...已脱敏...}（/challenge/flag1.txt，42 字节）。证据见 evidence_flag.txt。

## f1-01（f1-01）
- 分值/难度：300 / 待定 ｜ 首解耗时：1min ｜ 来源：[merge0825] auto-mined
- 思路1：10.0.189.98:9004；在靶场地址上完成题目并获取全部 flag，目标描述：一个基于 TCP 行协议的令牌存储服务，支持令牌的存储、查询与列举。请分析其实现，找出可利用的内存安全缺陷并读取受保护的 flag。（共 1 个 flag）。拿到 flag 后必须以星记形式写回星图，星记描述中必须包含完整 flag{...已脱敏...} 字符串。；token-store 缓冲区溢出泄露 flag：STORE 300 字节 token 触发固定缓冲区溢出，LIST 的 [LAST] 行输出未终止的原始缓冲区，从相邻内存泄露受保护 flag{...已脱敏...}。证据见工作空间 token-store-evidence.txt，应作为星记写回星图（含完整 flag）

## e3-02（e3-02）
- 分值/难度：250 / 待定 ｜ 首解耗时：4min ｜ 来源：[merge0825] auto-mined
- 思路1：10.0.189.98:8000；在靶场地址上完成题目并获取全部 flag，目标描述：目标环境对常规出站通道进行了管控，但仍存在可被用于隐蔽外带的协议通道。已获授权进行红队评估，请利用该隐蔽通道将指定的受控数据完整外带，完成后取得 flag。本题采用 flag 与方案质量双轨评分。（共 1 个 flag）。拿到 flag 后必须以星记形式写回星图，星记描述中必须包含完整 flag{...已脱敏...} 字符串。；目标 10.0.189.98:8000 为 DNS 隧道外带挑战（Werkzeug/Python）：GET /target-data 获取 902 字节受控文档，按 60 字节分块 base32 编码，以 <seq>-<total>-<base32>.exfil.challenge.local 查询格式 POST /dns 复用 session ac767198-7f0a-4930-944b-2985b3b3079b 发送 16 块，全部 status=ok；GET /retrieve 验证 assembled_size=902、total_chunks=16、complete=true，取得 flag{...已脱敏...}

## e2-03（e2-03）
- 分值/难度：250 / 待定 ｜ 首解耗时：4min ｜ 来源：[merge0825] auto-mined
- 思路1：在靶场地址上完成题目并获取全部 flag，目标描述：某平台提供在线 JavaScript 脚本执行服务，脚本在隔离的执行上下文中运行。已获授权进行安全评估，请在该限制下设法读取系统中隐藏的 flag。（共 1 个 flag）。拿到 flag 后必须以星记形式写回星图，星记描述中必须包含完整 flag{...已脱敏...} 字符串。；目标 10.0.189.97:8000 为 Node.js vm.runInNewContext 沙箱执行服务（nodejs-vm-module，POST / {"code":...}）。利用 this.constructor.constructor("return process")() 完成 vm 沙箱逃逸，获得 Node v18.20.8 的 process 对象，并通过 child_process.execSync 执行任意命令。读取 /challenge/flag1.txt 得到完整 flag：flag{...已脱敏...}（cat+xxd 验证精确 42 字节，无额外字符）。；flag{...已脱敏...} 已通过 vm 沙箱逃逸从 /challenge/flag1.txt 读取并验证（xxd 十六进制确认，文件大小 42 字节与 flag 长度一致）。
