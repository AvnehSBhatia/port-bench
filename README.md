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

Harnesses run one at a time, never at once, and the order alternates between tasks. Cost is what OpenRouter
billed, metered per request by `proxy.mjs`.

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
- it embeds the original Python (R5).

## Validation

- `python3 selftest.py` (seconds): 41 checks that every rule catches what it should and passes honest code.
- `python3 validate.py` (minutes): for every task:
  - the original Python library passes the full suite, which pins the expected count;
  - an empty attempt fails;
  - a wheel that ships the original Python passes the tests and is rejected by the rules;
  - embedded Python, re-exporting `tomllib`, a smuggled pytest plugin and a forbidden crate are each caught.

  It writes `VALIDATION.md`.

## Run it

Needs Rust, Python 3.11+, Node 22+, [Codex CLI](https://github.com/openai/codex) and
[Heliod Harness](https://heliod.dev).

```sh
export OPENROUTER_API_KEY=...
python3 validate.py           # once: check the benchmark itself
node run.mjs --preflight      # one tiny request per model: routing and keys work
node run.mjs --smoke          # 3 minutes per harness per task: each harness can drive each model
node run.mjs --all            # the benchmark
```

Results go to `results/<time>/`:
- `SCORECARD.md`;
- `results.json`;
- for each harness, the full transcript, the final workspace and `grade.json`, so anyone can check a result by hand.

## License

MIT
