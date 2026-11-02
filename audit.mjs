// Billing audit: asks OpenRouter about every request proxy.mjs metered, and checks what it billed and which provider
// served it. Anyone with the run's proxy.jsonl and the same OpenRouter account can repeat it.
//
//   OPENROUTER_API_KEY=... node audit.mjs results/<time>     -> AUDIT.md and AUDIT.json in that folder
//
// A call counts as confirmed when OpenRouter returns its generation record; the audit then compares OpenRouter's
// total_cost with the cost the proxy metered, and the provider that served it with the pin in models.json.
import { readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";

const HERE = import.meta.dirname;
const dir = process.argv[2];
if (!dir) throw new Error("usage: node audit.mjs results/<time>");
if (!process.env.OPENROUTER_API_KEY) throw new Error("OPENROUTER_API_KEY is not set");
const MODELS = JSON.parse(readFileSync(join(HERE, "models.json"), "utf8"));
const norm = (s) => String(s ?? "").toLowerCase().replace(/[^a-z0-9]/g, "");

const recs = readFileSync(join(dir, "proxy.jsonl"), "utf8")
	.split("\n")
	.filter(Boolean)
	.map((l) => JSON.parse(l))
	.filter((r) => typeof r.cost === "number" && r.arm !== "preflight");

async function lookup(id) {
	for (let attempt = 0; attempt < 4; attempt++) {
		const res = await fetch(`https://openrouter.ai/api/v1/generation?id=${encodeURIComponent(id)}`, { headers: { authorization: `Bearer ${process.env.OPENROUTER_API_KEY}` } });
		if (res.ok) return (await res.json()).data ?? null;
		if (res.status !== 404 && res.status !== 429 && res.status < 500) return null;
		await new Promise((r) => setTimeout(r, 1500 * (attempt + 1)));
	}
	return null;
}

const out = { calls: recs.length, confirmed: 0, metered_usd: 0, confirmed_usd: 0, provider_mismatches: 0, cost_mismatches: 0, unconfirmed: [], mismatches: [], arms: {} };
let next = 0;
async function worker() {
	while (next < recs.length) {
		const r = recs[next++];
		const a = (out.arms[r.arm] ??= { calls: 0, confirmed: 0, metered_usd: 0, confirmed_usd: 0 });
		a.calls++;
		a.metered_usd += r.cost;
		out.metered_usd += r.cost;
		const g = r.gen ? await lookup(r.gen) : null;
		if (!g) {
			out.unconfirmed.push(r.gen ?? `(no id) ${r.arm} ${r.t}`);
			continue;
		}
		const billed = Number(g.total_cost ?? g.usage ?? 0);
		out.confirmed++;
		a.confirmed++;
		out.confirmed_usd += billed;
		a.confirmed_usd += billed;
		const pin = MODELS[r.model]?.provider?.split("/")[0];
		if (pin && g.provider_name && !norm(g.provider_name).startsWith(norm(pin))) {
			out.provider_mismatches++;
			out.mismatches.push({ gen: r.gen, arm: r.arm, kind: "provider", pinned: MODELS[r.model].provider, served: g.provider_name });
		}
		if (Math.abs(billed - r.cost) > Math.max(1e-6, r.cost * 0.01)) {
			out.cost_mismatches++;
			out.mismatches.push({ gen: r.gen, arm: r.arm, kind: "cost", metered: r.cost, billed });
		}
	}
}
await Promise.all(Array.from({ length: 4 }, worker));

writeFileSync(join(dir, "AUDIT.json"), JSON.stringify(out, null, 2));
const md = [
	"# Billing audit",
	"",
	`OpenRouter confirmed ${out.confirmed} of ${out.calls} metered calls: $${out.confirmed_usd.toFixed(4)} billed against $${out.metered_usd.toFixed(4)} metered.`,
	`Provider other than the pin: ${out.provider_mismatches}. Cost differing from the metered cost by more than 1%: ${out.cost_mismatches}.`,
	"",
	"| Arm | Calls | Confirmed | Metered ($) | Billed by OpenRouter ($) |",
	"| --- | --- | --- | --- | --- |",
	...Object.entries(out.arms).map(([k, a]) => `| ${k} | ${a.calls} | ${a.confirmed} | ${a.metered_usd.toFixed(4)} | ${a.confirmed_usd.toFixed(4)} |`),
	"",
	out.unconfirmed.length ? `Not found at OpenRouter: ${out.unconfirmed.length} (listed in AUDIT.json).` : "Every call was found at OpenRouter.",
	"",
].join("\n");
writeFileSync(join(dir, "AUDIT.md"), md);
console.log(md);
