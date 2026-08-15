// ASTRA headless runner — DeepSeek Harness 单次运行插件（ASTRA 定制版）。
//
// 覆盖 @deepseek-ai/dsh-headless 的 runner，增加会话续接能力：
//   - 无 --session：与官方 headless 相同，创建全新会话（session-<uuid>）执行任务
//   - 有 --session：先 agents.resume() 从 $DSH_HOME/sessions 恢复持久化会话，
//     再提交任务——对应 claude 的 `-r <session>`，保证 ASTRA execute→conclude
//     双阶段共享模型探索上下文
//   - create-or-resume 语义：resume 不到则用**同一 id** 创建（对应 claude
//     `--session-id`），保证调用方持有的 session id 契约在 conclude 阶段一定能续上；
//     仅当同 id 创建也失败（磁盘状态异常）才退化为全新 id
//   - Windows：任务以 @<file> 传入时读取文件内容（ASTRA dsh 驱动的约定，
//     规避命令行长度/转义问题）
//
// 挂载方式：由 container/dsh/astra-headless.patch.yml 通过 --patch 注入，
// 替换 headless profile 的 headless-startup / headless-runner 两行。
//
// 本插件只使用 dsh 安装自带的 @deepseek-ai/* 依赖（与官方 runner 相同），
// 无额外 npm 依赖。

import { randomUUID } from "node:crypto";
import { appendFileSync, mkdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import z from "@deepseek-ai/schemastery";
import { installModelSelection } from "@deepseek-ai/dsh-agent";
import { createUserMessage } from "@deepseek-ai/dsh-llm";
import { SessionId } from "@deepseek-ai/dsh-session";

/** Stable Cordis plugin name. */
export const name = "astra-headless-runner";

/** Core services required before the one-shot turn can start. */
export const inject = ["cmdlineArgs", "agentDefaultModel", "agents", "sessions"];

export const Config = z.object({});

const USAGE = `Usage: dsh --profile headless [--session <id>] <task>

Answer one task, print the final assistant message, and exit.

Options:
  --session <id>   resume a persisted session (created by an earlier run)
  -h, --help       show this help
`;

/**
 * 解析内层参数：`--session <id>` 可选，其余参数拼接为任务文本。
 * @param argv - launcher 之后的应用参数（来自 ctx.cmdlineArgs.get()）。
 */
function parseInvocation(argv) {
	let session = null;
	const rest = [];
	for (let index = 0; index < argv.length; index += 1) {
		const arg = argv[index];
		if (arg === "--session") {
			session = argv[index + 1];
			if (session === undefined || session === "") {
				throw new Error("--session requires an id, for example: --session session-<uuid>");
			}
			index += 1;
		} else if (arg === "-h" || arg === "--help") {
			return { help: true, session: null, task: "" };
		} else {
			rest.push(arg);
		}
	}
	const task = rest.join(" ");
	if (task.trim() === "") {
		throw new Error("a task is required, for example: dsh --profile headless \"run the tests\"");
	}
	return { help: false, session, task };
}

/**
 * Windows 下任务以 @<file> 传入（ASTRA dsh 驱动约定）：存在该文件则读取内容；
 * 否则按字面任务处理。
 */
function expandAtFile(task) {
	if (!task.startsWith("@") || task.length <= 1) return task;
	const candidate = task.slice(1);
	try {
		return readFileSync(candidate, "utf8");
	} catch (error) {
		throw new Error(`cannot read prompt file ${candidate}: ${error instanceof Error ? error.message : String(error)}`);
	}
}

/** 聚合一次运行区间内的最后一条 assistant 文本与 turn 结局。 */
function summarize(events, firstSeq) {
	let started = false;
	let text = "";
	let reason;
	for (const event of events) {
		if (event.seq < firstSeq) continue;
		if (event.type === "turn/start") {
			started = true;
			continue;
		}
		if (!started) continue;
		if (event.type === "assistant/message") {
			const joined = event.data.message.content
				.filter((block) => block.type === "text")
				.map((block) => block.text)
				.join("");
			if (joined !== "") text = joined;
		}
		if (event.type === "turn/end") reason = event.data.reason;
	}
	return { text, reason };
}

/** 统计一次运行区间内的 token 用量（assistant/chunk 事件 data={turn,step,chunk}，
 * chunk.type==='usage' 时累加 chunk.usage 的 TokenUsage 字段）。 */
function collectUsage(events, firstSeq) {
	const total = {
		inputTokens: 0,
		outputTokens: 0,
		cacheReadTokens: 0,
		cacheWriteTokens: 0,
		reasoningTokens: 0
	};
	for (const event of events) {
		if (event.seq < firstSeq) continue;
		if (event.type !== "assistant/chunk") continue;
		const chunk = event.data?.chunk;
		if (chunk === undefined || chunk.type !== "usage" || chunk.usage === undefined) continue;
		const usage = chunk.usage;
		total.inputTokens += usage.inputTokens ?? 0;
		total.outputTokens += usage.outputTokens ?? 0;
		total.cacheReadTokens += usage.cacheReadTokens ?? 0;
		total.cacheWriteTokens += usage.cacheWriteTokens ?? 0;
		total.reasoningTokens += usage.reasoningTokens ?? 0;
	}
	return total;
}

/** 把本轮用量追加写入 $DSH_HOME/usage/astra-usage.jsonl（供 runner 汇总成本）。 */
function recordUsage(io, session, usage) {
	const home = process.env.DSH_HOME;
	if (!home || usage.inputTokens + usage.outputTokens <= 0) return;
	try {
		const dir = join(home, "usage");
		mkdirSync(dir, { recursive: true });
		const record = {
			ts: new Date().toISOString(),
			session,
			...usage
		};
		appendFileSync(join(dir, "astra-usage.jsonl"), JSON.stringify(record) + "\n", "utf8");
	} catch (error) {
		io.stderr.write(`dsh: usage record failed: ${error instanceof Error ? error.message : String(error)}\n`);
	}
}

function fail(io, error) {
	io.stderr.write(`dsh: ${error instanceof Error ? error.message : String(error)}\n`);
	io.exit(1);
}

/**
 * 执行一次任务。有 session id 时先尝试 resume（等价 claude -r）；
 * resume 不到则用**同一 id** 创建（等价 claude --session-id 的 create-or-resume
 * 语义，保证 execute 阶段派发的 id 在 conclude 阶段一定能续上）；
 * 仅在磁盘状态异常（如日志损坏导致同 id 创建也失败）时才退化为全新 id。
 * @param ctx - 持有 agent/会话/默认模型服务的插件上下文。
 * @param invocation - 解析后的 { session, task }。
 * @param io - 进程侧输出与退出。
 */
async function run(ctx, invocation, io) {
	await ctx.get("loader")?.await();
	const agents = ctx.get("agents");
	const defaultModel = ctx.get("agentDefaultModel");
	const sessions = ctx.get("sessions");
	if (agents === undefined || defaultModel === undefined || sessions === undefined) return;

	const selection = defaultModel.currentSelection();
	const agentOptions = {
		provider: selection.provider,
		model: selection.model
	};
	const setup = (agentCtx) => {
		installModelSelection(agentCtx, {
			current: selection,
			assembled: void 0
		});
	};

	let handle;
	if (invocation.session !== null) {
		try {
			handle = await agents.resume({
				resumeSessionId: SessionId(invocation.session),
				agentOptions,
				setup
			});
			io.stderr.write(`dsh: resumed session ${invocation.session}\n`);
		} catch (resumeError) {
			// 会话尚不存在（execute 首跑）或加载失败：用同一 id 创建，
			// 让调用方持有的 session id 契约不被破坏。
			io.stderr.write(
				`dsh: resume failed for ${invocation.session} (${resumeError instanceof Error ? resumeError.message : String(resumeError)}), creating session with the requested id\n`
			);
			try {
				handle = await agents.create({
					sessionId: SessionId(invocation.session),
					meta: { cwd: process.cwd() },
					agentOptions,
					setup
				});
			} catch (createError) {
				// 磁盘状态异常（极少见）：放弃契约 id，保证任务仍可执行。
				io.stderr.write(
					`dsh: create with requested id failed for ${invocation.session} (${createError instanceof Error ? createError.message : String(createError)}), starting a fresh session\n`
				);
				handle = await agents.create({
					sessionId: SessionId(`session-${randomUUID()}`),
					meta: { cwd: process.cwd() },
					agentOptions,
					setup
				});
			}
		}
	} else {
		handle = await agents.create({
			sessionId: SessionId(`session-${randomUUID()}`),
			meta: { cwd: process.cwd() },
			agentOptions,
			setup
		});
	}

	const { agent } = handle;
	await agent.whenIdle();
	const firstSeq = agent.session.seq;
	agent.followup(
		createUserMessage({
			content: [{ type: "text", text: invocation.task }],
			source: { kind: "user" }
		})
	);
	await agent.whenIdle();
	await sessions.flush(agent.session);

	const outcome = summarize(agent.session.events, firstSeq);
	recordUsage(io, String(agent.session.id), collectUsage(agent.session.events, firstSeq));
	io.stdout.write(outcome.text + "\n");
	if (outcome.reason?.kind === "error") {
		io.stderr.write(`dsh: ${outcome.reason.error.code}: ${outcome.reason.error.message}\n`);
	}
	io.exit(outcome.reason?.kind === "completed" ? 0 : 1);
}

/**
 * 挂载单次运行驱动：解析命令行 → 展开 @file → 执行 → 退出。
 * @param ctx - 插件上下文。
 * @param _config - 未使用（空 Config）。
 */
export function apply(ctx, _config) {
	const exit = ctx.get("appExit");
	if (exit === void 0) {
		throw new Error("astra-headless-runner: the launcher must provide ctx.appExit before the tree mounts");
	}
	const io = { stdout: process.stdout, stderr: process.stderr, exit };
	const argv = ctx.cmdlineArgs.get();
	let invocation;
	try {
		invocation = parseInvocation(argv);
		if (invocation.help) {
			io.stdout.write(USAGE);
			io.exit(0);
			return;
		}
		invocation.task = expandAtFile(invocation.task);
	} catch (error) {
		io.stderr.write(`dsh: ${error instanceof Error ? error.message : String(error)}\n`);
		io.exit(1);
		return;
	}
	run(ctx, invocation, io).catch((error) => {
		fail(io, error);
	});
}
