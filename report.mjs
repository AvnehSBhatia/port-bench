// Port Bench report: one table per library across trials, efficiency per passing test, and each harness's ratio to
// Heliod. Every number comes from results.json (and AUDIT.json when audit.mjs has run); nothing is estimated.
//
//   node report.mjs results/<time> [results/<time> ...]   -> REPORT.md (and report.json) in the first folder
//   node report.mjs --selftest                            -> checks the arithmetic on made-up rows
//
// Definitions:
//   works            the port passes every test the original library passes, and breaks no rule (grade.py)
//   tests passed     share of the original library's passing tests that the port passes
//   cost per test    billed dollars / tests passed: a harness cannot look cheap by stopping early
//   tokens per test  (input + output tokens) / tests passed
//   ratio            other harness / Heliod, on medians; above 1 means Heliod used less. Across libraries: geometric mean
import { existsSync, readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";

const median = (xs) => {
	const v = xs.filter((x) => Number.isFinite(x) || x === Infinity).sort((a, b) => a - b);
	if (!v.length) return null;
	const m = Math.floor(v.length / 2);
	return v.length % 2 ? v[m] : (v[m - 1] + v[m]) / 2;
};
const geomean = (xs) => {
	const v = xs.filter((x) => Number.isFinite(x) && x > 0);
	return v.length ? Math.exp(v.reduce((s, x) => s + Math.log(x), 0) / v.length) : null;
};
const perTest = (num, passed) => (passed > 0 ? num / passed : Infinity);
const fmt = (x, d = 2) => (x === null || x === undefined ? "-" : x === Infinity ? "∞" : Number(x).toFixed(d));
const times = (x) => (x === null ? "-" : x === Infinity ? "∞" : `${x >= 10 ? x.toFixed(0) : x.toFixed(1)}×`);

/** One cell per library × harness, over its trials. */
export function summarize(rows) {
	const cells = new Map();
	for (const r of rows) {
		const k = `${r.task}\u0000${r.arm}`;
		if (!cells.has(k)) cells.set(k, { task: r.task, arm: r.arm, model: r.model, runs: [] });
		cells.get(k).runs.push(r);
	}
	return [...cells.values()].map((c) => {
		const graded = c.runs.filter((r) => r.tests_passed !== null && r.tests_passed !== undefined);
		const tokens = (r) => (r.input_tokens ?? 0) + (r.output_tokens ?? 0);
		return {
			task: c.task,
			arm: c.arm,
			model: c.model,
			trials: c.runs.length,
			works: graded.filter((r) => r.works === true).length,
			graded: graded.length,
			share: median(graded.map((r) => (r.tests_expected ? r.tests_passed / r.tests_expected : null))),
			usd: median(c.runs.map((r) => r.billed_usd)),
			usd_range: [Math.min(...c.runs.map((r) => r.billed_usd)), Math.max(...c.runs.map((r) => r.billed_usd))],
			tokens: median(c.runs.map(tokens)),
			uncached: median(c.runs.map((r) => (r.input_tokens ?? 0) - (r.cached_tokens ?? 0))),
			output: median(c.runs.map((r) => r.output_tokens ?? 0)),
			minutes: median(c.runs.map((r) => r.minutes)),
			calls: median(c.runs.map((r) => r.calls)),
			usd_per_test: graded.length ? median(graded.map((r) => perTest(r.billed_usd, r.tests_passed))) : null,
			tokens_per_test: graded.length ? median(graded.map((r) => perTest(tokens(r), r.tests_passed))) : null,
		};
	});
}

/** Each other harness against Heliod, per library and across libraries. */
export function ratios(cells, base = "heliod") {
	const out = [];
	const arms = [...new Set(cells.map((c) => c.arm))].filter((a) => a !== base);
	for (const arm of arms) {
		const per = [];
		for (const h of cells.filter((c) => c.arm === base)) {
			const o = cells.find((c) => c.arm === arm && c.task === h.task);
			if (!o) continue;
			const r = (a, b) => (a === null || b === null || b === 0 ? null : a === Infinity ? Infinity : b === Infinity ? 0 : a / b);
			per.push({ task: h.task, usd: r(o.usd, h.usd), tokens: r(o.tokens, h.tokens), minutes: r(o.minutes, h.minutes), usd_per_test: r(o.usd_per_test, h.usd_per_test), tokens_per_test: r(o.tokens_per_test, h.tokens_per_test) });
		}
		const across = (k) => {
			const v = per.map((p) => p[k]);
			if (v.some((x) => x === Infinity)) return { value: geomean(v.filter((x) => x !== Infinity)), note: `${v.filter((x) => x === Infinity).length} library(ies) where ${arm} passed no test left out` };
			return { value: geomean(v), note: "" };
		};
		out.push({ arm, per, overall: { usd: across("usd"), tokens: across("tokens"), minutes: across("minutes"), usd_per_test: across("usd_per_test"), tokens_per_test: across("tokens_per_test") } });
	}
	return out;
}

/** Plain sentences, each stating its sample; "less" or "more" follows the data. */
export function headlines(cells, rs, base = "heliod") {
	const lines = [];
	const tasks = [...new Set(cells.map((c) => c.task))];
	const sum = (arm, k) => cells.filter((c) => c.arm === arm).reduce((s, c) => s + c[k], 0);
	const armsAll = [...new Set(cells.map((c) => c.arm))];
	lines.push(`Sample: ${tasks.length} libraries × ${Math.max(...cells.map((c) => c.trials))} trial(s) per harness.`);
	if (cells.some((c) => c.graded > 0)) lines.push(`Ports that work (every original test passes, no rule broken): ${armsAll.map((a) => `${a} ${sum(a, "works")}/${sum(a, "graded")}`).join(", ")}.`);
	else lines.push("Not graded (a smoke run): cost, tokens and time only.");
	for (const r of rs) {
		const say = (label, o, unit) => {
			if (!o.value) return;
			const less = unit.startsWith("money") ? "less" : "fewer";
			const word = o.value >= 1 ? `${times(o.value)} ${less} ${unit}` : `${times(1 / o.value)} more ${unit}`;
			lines.push(`${label}: ${base} used ${word} than ${r.arm} (geometric mean over ${r.per.length} libraries${o.note ? `; ${o.note}` : ""}; per library ${r.per.map((p) => `${p.task} ${times(p[label === "Cost per passing test" ? "usd_per_test" : label === "Tokens per passing test" ? "tokens_per_test" : label === "Billed cost" ? "usd" : "tokens"])}`).join(", ")}).`);
		};
		say("Cost per passing test", r.overall.usd_per_test, "money per passing test");
		say("Tokens per passing test", r.overall.tokens_per_test, "tokens per passing test");
		say("Billed cost", r.overall.usd, "money");
		say("Tokens", r.overall.tokens, "tokens");
	}
	return lines;
}

function render(cells, rs, audit, sources) {
	const md = [];
	md.push("# Port Bench report", "");
	md.push(...headlines(cells, rs).map((l) => `- ${l}`), "");
	if (audit) md.push(`Billing audit: OpenRouter confirmed ${audit.confirmed} of ${audit.calls} metered calls ($${fmt(audit.confirmed_usd, 4)} of $${fmt(audit.metered_usd, 4)} metered), ${audit.provider_mismatches} served by a provider other than the pin.`, "");
	md.push("## Per library (medians over trials)", "");
	md.push("| Library | Harness | Works | Tests passed | Billed ($, min-max) | Tokens (uncached in / out) | Minutes | $ per 100 passing tests | Tokens per passing test |");
	md.push("| --- | --- | --- | --- | --- | --- | --- | --- | --- |");
	for (const c of cells.sort((a, b) => a.task.localeCompare(b.task) || a.arm.localeCompare(b.arm))) {
		md.push(
			`| ${c.task} | ${c.arm} | ${c.graded ? `${c.works}/${c.graded}` : "-"} | ${c.share === null ? "-" : `${(c.share * 100).toFixed(1)}%`} | ${fmt(c.usd, 3)} (${fmt(c.usd_range[0], 3)}-${fmt(c.usd_range[1], 3)}) | ${(c.tokens / 1e6).toFixed(2)}M (${(c.uncached / 1e3).toFixed(0)}k / ${(c.output / 1e3).toFixed(0)}k) | ${fmt(c.minutes, 1)} | ${c.usd_per_test === null ? "-" : fmt(c.usd_per_test * 100, 3)} | ${c.tokens_per_test === null ? "-" : fmt(c.tokens_per_test, 0)} |`,
		);
	}
	md.push("", "## Ratios to Heliod (other / Heliod; above 1 means Heliod used less)", "");
	md.push("| Harness | Library | Cost | Tokens | Minutes | Cost per passing test | Tokens per passing test |", "| --- | --- | --- | --- | --- | --- | --- |");
	for (const r of rs) {
		for (const p of r.per) md.push(`| ${r.arm} | ${p.task} | ${times(p.usd)} | ${times(p.tokens)} | ${times(p.minutes)} | ${times(p.usd_per_test)} | ${times(p.tokens_per_test)} |`);
		md.push(`| ${r.arm} | **all (geometric mean)** | ${times(r.overall.usd.value)} | ${times(r.overall.tokens.value)} | ${times(r.overall.minutes.value)} | ${times(r.overall.usd_per_test.value)} | ${times(r.overall.tokens_per_test.value)} |`);
	}
	md.push("", `Source: ${sources.join(", ")}. Definitions at the top of report.mjs.`, "");
	return md.join("\n");
}

function selftest() {
	const row = (task, arm, trial, usd, inTok, outTok, passed, expected, works) => ({ task, arm, trial, model: "m", billed_usd: usd, input_tokens: inTok, cached_tokens: inTok / 2, output_tokens: outTok, minutes: 10, calls: 5, tests_passed: passed, tests_expected: expected, works });
	const rows = [
		row("a", "heliod", 1, 0.1, 900, 100, 100, 100, true),
		row("a", "heliod", 2, 0.3, 900, 100, 100, 100, true),
		row("a", "codex", 1, 1.0, 9000, 1000, 100, 100, true),
		row("a", "codex", 2, 2.0, 9000, 1000, 50, 100, false),
		row("b", "heliod", 1, 0.5, 4000, 1000, 10, 10, true),
		row("b", "codex", 1, 0.5, 4000, 1000, 0, 10, false),
	];
	const cells = summarize(rows);
	const ha = cells.find((c) => c.task === "a" && c.arm === "heliod");
	const ca = cells.find((c) => c.task === "a" && c.arm === "codex");
	const fails = [];
	const eq = (a, b, m) => (Math.abs(a - b) < 1e-9 ? null : fails.push(`${m}: ${a} != ${b}`));
	eq(ha.usd, 0.2, "median cost");
	eq(ha.works, 2, "works count");
	eq(ca.usd_per_test, (0.01 + 0.04) / 2, "cost per passing test, median over trials");
	eq(ca.share, 0.75, "median share of tests");
	const rs = ratios(cells);
	const a = rs[0].per.find((p) => p.task === "a");
	eq(a.usd, 1.5 / 0.2, "cost ratio");
	eq(a.tokens, 10000 / 1000, "token ratio");
	const b = rs[0].per.find((p) => p.task === "b");
	if (b.usd_per_test !== Infinity) fails.push("a harness that passed no test has an infinite cost per test");
	eq(rs[0].overall.usd_per_test.value, a.usd_per_test, "an infinite ratio is left out of the mean, and said so");
	if (!/1 library\(ies\) where codex passed no test left out/.test(rs[0].overall.usd_per_test.note)) fails.push("the note says what was left out");
	const h = headlines(cells, rs).join("\n");
	if (!/heliod 3\/3, codex 1\/3|codex 1\/3, heliod 3\/3/.test(h)) fails.push(`works line: ${h}`);
	if (!/Billed cost: heliod used .*less money than codex/.test(h)) fails.push(`direction word: ${h}`);
	const flip = ratios(summarize(rows.map((r) => ({ ...r, billed_usd: r.arm === "heliod" ? r.billed_usd * 100 : r.billed_usd }))));
	if (!/more money/.test(headlines(summarize(rows), flip).join("\n"))) fails.push("when Heliod costs more, the headline says more");
	console.log(fails.length ? `report selftest: ${fails.length} failure(s)\n  ${fails.join("\n  ")}` : "report selftest: ok (9 checks)");
	process.exit(fails.length ? 1 : 0);
}

if (process.argv[2] === "--selftest") selftest();
else if (process.argv[1] && import.meta.filename === process.argv[1]) {
	const dirs = process.argv.slice(2);
	if (!dirs.length) throw new Error("usage: node report.mjs results/<time> [...]");
	const rows = dirs.flatMap((d) => JSON.parse(readFileSync(join(d, "results.json"), "utf8")).rows);
	const audits = dirs.map((d) => join(d, "AUDIT.json")).filter(existsSync).map((f) => JSON.parse(readFileSync(f, "utf8")));
	const audit = audits.length
		? audits.reduce((s, a) => ({ calls: s.calls + a.calls, confirmed: s.confirmed + a.confirmed, metered_usd: s.metered_usd + a.metered_usd, confirmed_usd: s.confirmed_usd + a.confirmed_usd, provider_mismatches: s.provider_mismatches + a.provider_mismatches }))
		: null;
	const cells = summarize(rows);
	const rs = ratios(cells);
	writeFileSync(join(dirs[0], "REPORT.md"), render(cells, rs, audit, dirs));
	writeFileSync(join(dirs[0], "report.json"), JSON.stringify({ cells, ratios: rs, headlines: headlines(cells, rs), audit }, null, 2));
	console.log(headlines(cells, rs).join("\n"));
}
