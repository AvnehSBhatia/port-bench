# Port Bench

Port Bench measures how well a coding agent rewrites a real Python library in Rust, and what that costs.

The agent gets a well-known open-source Python library and one instruction: port it to Rust with PyO3 so that
`import <library>` loads the Rust code. It is graded by the library's own test suite, unmodified, run against the
compiled build. Each attempt reports:

- **Works:** the port passes every test the original Python library passes, and follows the rules.
- **Tests passed:** the share of the original tests that pass, which shows partial progress.
- **Time:** minutes from start until the agent stopped.
- **Cost:** what OpenRouter billed, in dollars.
- **Tokens:** input, cached and output tokens, and the number of model calls.
- **Per passing test:** dollars and tokens divided by the tests the port passes, so a harness cannot look cheap by
  stopping early.

## Tasks

| Library | What it is | Python | Graded by | Model | Cap per harness |
| --- | --- | --- | --- | --- | --- |
| [tomli](https://github.com/hukkin/tomli) | The TOML parser that became Python's `tomllib` | ~1.1k lines | Its tests, including 744 toml-test cases | Claude Haiku 5.5 | $2.50, 2 h |
| [jmespath.py](https://github.com/jmespath/jmespath.py) | JMESPath, the JSON query language in the AWS CLI | ~1.9k lines | The official JMESPath compliance suite | GPT-6 Luna | $2.50, 2 h |
| [python-tabulate](https://github.com/astanin/python-tabulate) | Pretty-printed tables | ~3.1k lines | Its public-API tests | DeepSeek V4.1 Flash | $2.50, 2 h |

Commits are pinned in `tasks.json`. Each task runs on a different model, so one run covers three model families.

## Fair comparison

Every harness gets:
- the same starting repository;
- the same prompt: "Do the task described in TASK.md in this repository";
- the same model, routed through OpenRouter to one pinned provider with no fallbacks;
- medium reasoning, the same caps, and permission to run any command.

Harnesses run one at a time, never at once. Each task runs several trials (`--trials`, 3 for published numbers),
and which harness goes first rotates with the task and the trial. Cost is what OpenRouter billed, metered per
request by `proxy.mjs`. Codex runs with subagents on; `--arms codex,codex-solo,heliod` adds Codex without them.

## Checked billing

After a run, `audit.mjs` looks up every metered request at OpenRouter by its generation id and checks:
- what OpenRouter billed, against what the proxy metered;
- which provider served it, against the pin in `models.json`.

It writes `AUDIT.md`. Anyone with the run's `proxy.jsonl` and access to the same OpenRouter account can repeat it.

## No cheating

The original Python code sits in `reference/` for the agent to read. The grader:
- removes `reference/` before building;
- builds the wheel in a fresh environment;
- hides the workspace while the tests run;
- runs a pristine copy of the tests from outside the repository, with pytest plugins from packages disabled.

A port does not count if:
- it depends on a Rust crate that already implements the library (R1);
- its wheel ships anything beyond the package (R2);
- its Python files hold more than imports and constants (R3);
- its Rust code runs Python source (R4);
- it embeds the original Python (R5);
- it stores the tests' answers: 5 or more of the test suite's long strings (expected outputs, inputs, data files)
  that the original library does not itself contain (R6).

## Validation

- `python3 selftest.py` (seconds): 46 checks that every rule catches what it should and passes honest code.
- `node report.mjs --selftest` (instant): the report's arithmetic on made-up results.
- `python3 validate.py` (minutes): for every task:
  - the original Python library passes the full suite, which pins the expected count;
  - an empty attempt fails;
  - a wheel that ships the original Python passes the tests and is rejected by the rules;
  - embedded Python, re-exporting `tomllib`, a smuggled pytest plugin, stored test answers and a forbidden crate
    are each caught.

  It writes `VALIDATION.md`.

## Run it

Needs Rust, Python 3.11+, Node 22+, [Codex CLI](https://github.com/openai/codex) and
[Heliod Harness](https://heliod.dev).

```sh
export OPENROUTER_API_KEY=...
python3 validate.py           # once: check the benchmark itself
node run.mjs --preflight      # one tiny request per model: routing and keys work
node run.mjs --smoke          # 3 minutes per harness per task: each harness can drive each model
node run.mjs --all --trials 3 # the benchmark
```

Results go to `results/<time>/`:
- `REPORT.md`: medians over trials, the cost and tokens per passing test, and each harness's ratio to Heliod;
- `AUDIT.md`: the billing check;
- `SCORECARD.md`: one row per attempt;
- `results.json`, with a manifest: tool versions, this repository's commit, and hashes of the files that define
  the benchmark;
- for each attempt, the full transcript, the final workspace and `grade.json`, so anyone can check a result by hand.

## Limits

- These libraries are well known, so models have seen their Python source. Both harnesses get the same model, so
  this does not favour either, but it does make the tasks easier than porting unknown code.
- Three libraries is a small sample. The report states the sample size next to every number.
- One prompt and medium reasoning for every harness. Other settings may change the results.

## License

MIT
