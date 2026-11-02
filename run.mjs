// Port Bench runner: Codex CLI vs Heliod Harness, one library at a time, one harness at a time.
//
//   OPENROUTER_API_KEY=... node run.mjs --preflight         # one tiny request per model on both APIs; checks routing
//   OPENROUTER_API_KEY=... node run.mjs --smoke             # 3 min / 5 cents per harness per task: can each drive each model?
//   OPENROUTER_API_KEY=... node run.mjs --task tomli        # one task, both harnesses
//   OPENROUTER_API_KEY=... node run.mjs --all               # every task
//   options: --arms codex,heliod   --allow-any-heliod   --suite <dir> (a folder with its own tasks.json, prepare.py, grade.py)
//
// Harnesses run one after the other, never at the same time, so their requests never overlap at OpenRouter or the
// provider. The order alternates by task (codex first on the 1st and 3rd task, heliod first on the 2nd) so neither
// always goes first. Each harness gets: the same workspace (prepare.py), the same one-line prompt pointing at TASK.md,
// the task's model through OpenRouter pinned to one provider (proxy.mjs), medium reasoning, freedom to run any
// command, network (cargo and pip need it), and the task's dollar and time caps. proxy.mjs meters what OpenRouter
// bills. When a harness stops, grade.py builds and tests its port. Results: results/<time>/results.json, SCORECARD.md.
import { spawn, spawnSync } from "node:child_process";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { join, resolve } from "node:path";

const HERE = import.meta.dirname;
const args = Object.fromEntries(process.argv.slice(2).reduce((acc, a, i, all) => (a.startsWith("--") ? [...acc, [a.slice(2), all[i + 1] && !all[i + 1].startsWith("--") ? all[i + 1] : "1"]] : acc), []));
const SUITE = args.suite ? resolve(args.suite) : HERE;
const TASKS = JSON.parse(readFileSync(join(SUITE, "tasks.json"), "utf8"));
const MODELS = JSON.parse(readFileSync(join(HERE, "models.json"), "utf8"));
const CODEX = process.env.CODEX_BIN ?? join(process.env.HOME, ".local/bin/codex");
const HELIOD = process.env.HELIOD_MAIN ?? join(process.env.HOME, ".heliod/lib/node_modules/heliod-harness/dist/main.js");
const EFFORT = "medium";
const PORT = 8797;
const PROMPT = "Do the task described in TASK.md in this repository.";
const ARMS = (args.arms ?? "codex,heliod").split(",");
if (!process.env.OPENROUTER_API_KEY) throw new Error("OPENROUTER_API_KEY is not set");

const stamp = new Date().toISOString().replace(/[:.]/g, "-");
const OUT = join(HERE, "results", stamp);
mkdirSync(OUT, { recursive: true });

const version = (cmd, a) => (spawnSync(cmd, a, { encoding: "utf8" }).stdout ?? "").trim().split("\n").pop();
const VERSIONS = { codex: version(CODEX, ["--version"]), heliod: version(process.execPath, [HELIOD, "--version"]) };

function startProxy(budget) {
	const p = spawn(process.execPath, [join(HERE, "proxy.mjs")], { env: { ...process.env, PROXY_PORT: String(PORT), OUT, BUDGET_USD: String(budget) }, stdio: ["ignore", "ignore", "inherit"] });
	return new Promise((r) => setTimeout(() => r(p), 800));
}
const spend = async () => (await (await fetch(`http://127.0.0.1:${PORT}/spend`)).json()).arms;

// One tiny request per API (Codex speaks the Responses API, Heliod chat completions) through the proxy's pinned route.
async function preflight(model) {
	const base = `http://127.0.0.1:${PORT}/r/preflight/api/v1`;
	const headers = { "content-type": "application/json", authorization: `Bearer ${process.env.OPENROUTER_API_KEY}` };
	const checks = [
		["chat/completions", { model, messages: [{ role: "user", content: "Reply with the word ok." }], max_tokens: 64 }],
		["responses", { model, input: "Reply with the word ok.", max_output_tokens: 64 }],
	];
	const problems = [];
	for (const [path, body] of checks) {
		const r = await fetch(`${base}/${path}`, { method: "POST", headers, body: JSON.stringify(body) });
		const j = await r.json().catch(() => ({}));
		if (!r.ok || j.error) problems.push(`${path}: HTTP ${r.status} ${JSON.stringify(j.error ?? j).slice(0, 200)}`);
		else if (j.provider && !j.provider.toLowerCase().startsWith(MODELS[model].provider)) problems.push(`${path}: served by ${j.provider}, expected ${MODELS[model].provider}`);
	}
	return problems;
}

function arm(name, model, ws, dir, key) {
	const base = `http://127.0.0.1:${PORT}/r/${key}/api/v1`;
	const env = { ...process.env, NO_COLOR: "1" };
	if (name === "codex") {
		const home = join(dir, "codexhome");
		mkdirSync(home, { recursive: true });
		return {
			cmd: CODEX,
			args: [
				"exec", "--json", "--skip-git-repo-check", "--ignore-user-config", "--cd", ws,
				"--sandbox", "workspace-write", "-c", "sandbox_workspace_write.network_access=true",
				"-m", model,
				"-c", `model_provider="openrouter"`, "-c", `model_providers.openrouter.name="OpenRouter"`,
				"-c", `model_providers.openrouter.base_url="${base}"`, "-c", `model_providers.openrouter.env_key="OPENROUTER_API_KEY"`,
				"-c", `model_providers.openrouter.wire_api="responses"`,
				"-c", `model_reasoning_effort="${EFFORT}"`, "-c", "features.multi_agent=true",
				"-c", `approval_policy="never"`, "-c", "check_for_update_on_startup=false",
				"-c", `projects."${ws}".trust_level="trusted"`,
				PROMPT,
			],
			env: { ...env, CODEX_HOME: home },
		};
	}
	const home = join(dir, "heliodhome");
	mkdirSync(join(home, "agent"), { recursive: true });
	const models = Object.entries(MODELS).map(([id, m]) => ({ id, name: id, api: "openai-completions", reasoning: true, input: ["text"], contextWindow: 1000000, maxTokens: 32768, cost: m.price }));
	writeFileSync(join(home, "agent/models.json"), JSON.stringify({ providers: { openrouter: { baseUrl: base, api: "openai-completions", apiKey: "$OPENROUTER_API_KEY", models } } }, null, 2));
	return {
		cmd: process.execPath,
		args: [HELIOD, "-p", "--mode", "json", "--model", `openrouter/${model}`, "--thinking", EFFORT, PROMPT],
		env: { ...env, HELIOD_HOME: home, HELIOD_PERMISSION_MODE: "full" },
	};
}

function runArm(name, t, budget, maxMs) {
	const dir = join(OUT, t.id, name);
	mkdirSync(dir, { recursive: true });
	const ws = join(dir, "ws");
	const prep = spawnSync("python3", [join(SUITE, "prepare.py"), t.id, ws], { encoding: "utf8" });
	if (prep.status !== 0) throw new Error(`prepare ${t.id}: ${prep.stderr}`);
	const key = `${name}-${t.id}`;
	const a = arm(name, t.model, ws, dir, key);
	return new Promise((resolve) => {
		const t0 = Date.now();
		const child = spawn(a.cmd, a.args, { cwd: ws, env: a.env, stdio: ["ignore", "pipe", "pipe"], detached: true });
		let out = "";
		let err = "";
		child.stdout.on("data", (c) => (out += c));
		child.stderr.on("data", (c) => (err += c));
		let why = "finished";
		const kill = (reason) => {
			if (why !== "finished") return;
			why = reason;
			try {
				process.kill(-child.pid, "SIGINT");
			} catch {}
			setTimeout(() => {
				try {
					process.kill(-child.pid, "SIGKILL");
				} catch {}
			}, 15_000);
		};
		const tick = setInterval(async () => {
			try {
				const s = (await spend())[key];
				if (s && s.usd >= budget) kill("budget");
			} catch {}
			if (Date.now() - t0 > maxMs) kill("time");
		}, 3000);
		child.on("close", () => {
			clearInterval(tick);
			writeFileSync(join(dir, "stdout.jsonl"), out);
			writeFileSync(join(dir, "stderr.log"), err);
			resolve({ name, key, ws, dir, why, minutes: (Date.now() - t0) / 60000 });
		});
	});
}

async function runTask(t, { smoke }) {
	const budget = smoke ? 0.05 : t.budget_usd;
	const maxMs = (smoke ? 3 : t.max_minutes) * 60_000;
	const proxy = await startProxy(budget);
	const rows = [];
	try {
		const problems = await preflight(t.model);
		if (problems.length) {
			console.log(`${t.id}: preflight failed for ${t.model}, skipping:\n  ${problems.join("\n  ")}`);
			return rows;
		}
		const order = TASKS.indexOf(t) % 2 === 0 ? ARMS : [...ARMS].reverse();
		console.log(`${t.id}: ${t.model} via ${MODELS[t.model].provider}, $${budget} and ${maxMs / 60000} min per harness, ${order.join(" then ")}`);
		for (const name of order) {
			const d = await runArm(name, t, budget, maxMs);
			const m = (await spend())[d.key] ?? {};
			let grade = null;
			if (!smoke) {
				const g = spawnSync("python3", [join(SUITE, "grade.py"), t.id, d.ws], { encoding: "utf8", maxBuffer: 1 << 26 });
				try {
					grade = JSON.parse(g.stdout.trim().split("\n").pop());
				} catch {
					grade = { works: false, build: `grader failed: ${(g.stderr || "").slice(-300)}` };
				}
				writeFileSync(join(d.dir, "grade.json"), JSON.stringify(grade, null, 2));
			}
			const cases = grade && (grade.subtests_passed || grade.subtests_failed) ? ` (cases ${grade.subtests_passed}/${grade.subtests_passed + grade.subtests_failed})` : "";
			const row = {
				task: t.id,
				arm: d.name,
				model: t.model,
				provider: MODELS[t.model].provider,
				version: VERSIONS[d.name],
				works: grade?.works ?? null,
				tests: grade ? `${grade.passed}/${grade.expected_passed ?? grade.total}${cases}` : null,
				rules: grade?.rules ?? [],
				build: grade?.build ?? null,
				minutes: Number(d.minutes.toFixed(1)),
				billed_usd: Number((m.usd ?? 0).toFixed(4)),
				calls: m.calls ?? 0,
				input_tokens: m.input ?? 0,
				cached_tokens: m.cached ?? 0,
				output_tokens: m.output ?? 0,
				cache_hit: m.input ? Number((m.cached / m.input).toFixed(3)) : 0,
				refused_calls: m.refused ?? 0,
				stopped_by: d.why,
				...(grade?.detail?.blocks ? { blocks: grade.detail.blocks } : {}),
			};
			rows.push(row);
			console.log(`  ${row.arm}: works ${row.works}, tests ${row.tests}, ${row.minutes} min, $${row.billed_usd}, cache ${row.cache_hit}, stopped by ${row.stopped_by}`);
		}
	} finally {
		proxy.kill();
	}
	return rows;
}

async function freezeCheck() {
	if (args["allow-any-heliod"]) return;
	const live = await (await fetch("https://heliod.dev/harness/latest.json")).json().catch(() => null);
	const local = (VERSIONS.heliod.match(/\d+\.\d+\.\d+/) ?? [])[0];
	if (!live || live.version !== local) {
		throw new Error(`Heliod Harness here is ${local}, heliod.dev serves ${live?.version}. Run \`heliod update\` (or pass --allow-any-heliod).`);
	}
}

if (args.preflight) {
	const proxy = await startProxy(1);
	for (const model of Object.keys(MODELS)) {
		const p = await preflight(model);
		console.log(`${model} via ${MODELS[model].provider}: ${p.length ? `FAILED\n  ${p.join("\n  ")}` : "ok (chat completions and responses)"}`);
	}
	console.log(`spent: ${JSON.stringify((await spend()).preflight ?? {})}`);
	proxy.kill();
	process.exit(0);
}

await freezeCheck();
const pick = args.all || args.smoke ? TASKS : TASKS.filter((t) => t.id === args.task);
if (!pick.length) throw new Error("use --preflight, --smoke, --task <id> or --all");
console.log(`${VERSIONS.codex}; Heliod Harness ${VERSIONS.heliod}. Results in ${OUT}`);
const rows = [];
for (const t of pick) {
	rows.push(...(await runTask(t, { smoke: Boolean(args.smoke) })));
	writeFileSync(join(OUT, "results.json"), JSON.stringify({ effort: EFFORT, smoke: Boolean(args.smoke), versions: VERSIONS, rows }, null, 2));
}
const md = [
	"| Library | Model | Harness | Works | Tests passed | Time (min) | Billed ($) | Input / cached / output tokens | Stopped by |",
	"| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
	...rows.map((r) => `| ${r.task} | ${r.model} | ${r.arm} | ${r.works === null ? "-" : r.works ? "yes" : "no"} | ${r.tests ?? "-"} | ${r.minutes} | ${r.billed_usd} | ${(r.input_tokens / 1e6).toFixed(2)}M / ${(r.cached_tokens / 1e6).toFixed(2)}M / ${(r.output_tokens / 1e3).toFixed(0)}k | ${r.stopped_by} |`),
].join("\n");
const blockRows = rows.filter((r) => r.blocks);
const blocks = blockRows.length
	? [
			"",
			`| Task | Harness | ${Object.keys(blockRows[0].blocks).map((k) => `${k} done / rules kept`).join(" | ")} |`,
			`| --- | --- | ${Object.keys(blockRows[0].blocks).map(() => "---").join(" | ")} |`,
			...blockRows.map((r) => `| ${r.task} | ${r.arm} | ${Object.values(r.blocks).map((b) => `${b.done}/10, ${Math.round(b.rules_kept * 100)}%`).join(" | ")} |`),
		].join("\n")
	: "";
writeFileSync(join(OUT, "SCORECARD.md"), `${md}\n${blocks}\n\n${VERSIONS.codex}; Heliod Harness ${VERSIONS.heliod}; reasoning ${EFFORT}.\n`);
console.log(`\n${md}\nresults: ${OUT}`);
