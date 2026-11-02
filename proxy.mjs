// Metering proxy. Every model request from every harness goes through here, and both harnesses get the same treatment.
//
//   http://127.0.0.1:<port>/r/<arm>/<openrouter path>  ->  https://openrouter.ai/<openrouter path>
//
// For every request it:
//   - pins the OpenRouter route to the model's provider from models.json, with no fallbacks, so both harnesses hit the
//     same provider (and its prompt cache) at the same price; a model missing from models.json is refused (HTTP 400);
//   - asks for usage on streamed chat completions (the Responses API always sends it);
//   - adds up the cost OpenRouter reports for each response (usage.cost: what OpenRouter bills);
//   - refuses new requests from an arm once its billed cost reaches the budget (HTTP 402).
// One JSON line per request goes to <OUT>/proxy.jsonl. GET /spend returns the totals per arm.
import http from "node:http";
import https from "node:https";
import { appendFileSync, mkdirSync, readFileSync } from "node:fs";
import { join } from "node:path";

const PORT = Number(process.env.PROXY_PORT ?? 8797);
const OUT = process.env.OUT ?? join(import.meta.dirname, "out");
const BUDGET = Number(process.env.BUDGET_USD ?? 1);
const MODELS = JSON.parse(readFileSync(join(import.meta.dirname, "models.json"), "utf8"));
mkdirSync(OUT, { recursive: true });
const LOG = join(OUT, "proxy.jsonl");
const log = (o) => appendFileSync(LOG, JSON.stringify({ t: Date.now(), ...o }) + "\n");

/** arm -> { usd, calls, input, cached, cacheWrite, output, refused, inflight, models } */
const spend = {};
const arm = (a) => (spend[a] ??= { usd: 0, calls: 0, input: 0, cached: 0, cacheWrite: 0, output: 0, refused: 0, inflight: 0, models: {} });

function usageOf(text, sse) {
	let usage = null;
	const visit = (o) => {
		if (!o || typeof o !== "object") return;
		if (o.type === "response.completed" && o.response?.usage) usage = o.response.usage;
		else if (o.usage && typeof o.usage === "object" && !o.type) usage = o.usage;
	};
	if (sse) {
		for (const line of text.split("\n")) {
			if (!line.startsWith("data:")) continue;
			const d = line.slice(5).trim();
			if (!d || d === "[DONE]") continue;
			try {
				visit(JSON.parse(d));
			} catch {}
		}
	} else {
		try {
			visit(JSON.parse(text));
		} catch {}
	}
	if (!usage) return null;
	const details = usage.prompt_tokens_details ?? usage.input_tokens_details ?? {};
	return {
		input: usage.prompt_tokens ?? usage.input_tokens ?? 0,
		cached: details.cached_tokens ?? 0,
		cacheWrite: details.cache_write_tokens ?? 0,
		output: usage.completion_tokens ?? usage.output_tokens ?? 0,
		cost: typeof usage.cost === "number" ? usage.cost : null,
	};
}

// Only used when a response carries no cost: the model's list price from models.json (per 1M tokens).
function priceOf(model, u) {
	const p = MODELS[model]?.price;
	if (!p) return 0;
	return ((u.input - u.cached - u.cacheWrite) * p.input + u.cached * p.cacheRead + u.cacheWrite * p.cacheWrite + u.output * p.output) / 1e6;
}

const server = http.createServer((req, res) => {
	if (req.method === "GET" && req.url === "/spend") {
		res.writeHead(200, { "content-type": "application/json" });
		return res.end(JSON.stringify({ budget: BUDGET, arms: spend }));
	}
	const m = /^\/r\/([\w.-]+)\/(.*)$/.exec(req.url ?? "");
	if (!m) {
		res.writeHead(404);
		return res.end("unknown route");
	}
	const [, a, rest] = m;
	const s = arm(a);
	// Calls already in flight will add their cost too: stop admitting new ones once the expected total reaches the budget.
	// 2x the average: calls late in a run are larger than the run's average, and in-flight calls finish after the cut.
	const avg = s.calls ? s.usd / s.calls : 0;
	if (s.usd + s.inflight * avg * 2 >= BUDGET) {
		s.refused++;
		log({ arm: a, path: "/" + rest, refused: true, usd: s.usd });
		res.writeHead(402, { "content-type": "application/json" });
		return res.end(JSON.stringify({ error: { message: `Budget of $${BUDGET} reached.`, code: 402 } }));
	}
	const chunks = [];
	req.on("data", (c) => chunks.push(c));
	req.on("end", () => {
		let body = Buffer.concat(chunks);
		let model = null;
		let cacheHints = null;
		if (body.length && String(req.headers["content-type"] ?? "").includes("json")) {
			let j = null;
			try {
				j = JSON.parse(body.toString("utf8"));
			} catch {}
			if (j && typeof j === "object") {
				model = j.model ?? null;
				if (model !== null) {
					const route = MODELS[model];
					if (!route) {
						log({ arm: a, path: "/" + rest, refused: true, reason: `model ${model} is not in models.json` });
						res.writeHead(400, { "content-type": "application/json" });
						return res.end(JSON.stringify({ error: { message: `port-bench: model ${model} is not in models.json`, code: 400 } }));
					}
					j.provider = { ...(j.provider ?? {}), order: [route.provider], allow_fallbacks: false };
				}
				cacheHints = { key: j.prompt_cache_key ?? null, retention: j.prompt_cache_retention ?? null };
				if (j.stream && rest.includes("chat/completions")) j.stream_options = { ...(j.stream_options ?? {}), include_usage: true };
				body = Buffer.from(JSON.stringify(j));
			}
		}
		const headers = { ...req.headers, host: "openrouter.ai", "accept-encoding": "identity" };
		delete headers.connection;
		headers["content-length"] = String(body.length);
		const t0 = Date.now();
		s.inflight++;
		const up = https.request({ host: "openrouter.ai", port: 443, method: req.method, path: "/" + rest, headers }, (ur) => {
			res.writeHead(ur.statusCode ?? 502, ur.headers);
			const sse = String(ur.headers["content-type"] ?? "").includes("event-stream");
			const got = [];
			ur.on("data", (c) => {
				got.push(c);
				res.write(c);
			});
			ur.on("end", () => {
				res.end();
				s.inflight--;
				const text = Buffer.concat(got).toString("utf8");
				const u = usageOf(text, sse);
				const rec = { arm: a, path: "/" + rest, status: ur.statusCode, ms: Date.now() - t0, model, cacheHints };
				if (u) {
					const cost = u.cost ?? priceOf(model, u);
					s.usd += cost;
					s.calls++;
					s.input += u.input;
					s.cached += u.cached;
					s.cacheWrite += u.cacheWrite;
					s.output += u.output;
					if (model) s.models[model] = (s.models[model] ?? 0) + 1;
					Object.assign(rec, u, { cost, costSource: u.cost === null ? "price-table" : "openrouter", usd: s.usd });
				} else if ((ur.statusCode ?? 0) >= 400) rec.error = text.slice(0, 300);
				log(rec);
			});
		});
		up.on("error", (e) => {
			s.inflight--;
			log({ arm: a, path: "/" + rest, error: String(e) });
			try {
				res.writeHead(502);
				res.end(String(e));
			} catch {}
		});
		up.end(body);
	});
});
server.listen(PORT, "127.0.0.1", () => console.log(`proxy on 127.0.0.1:${PORT}, budget $${BUDGET} per arm, log ${LOG}`));
