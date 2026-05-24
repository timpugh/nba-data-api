# NBA Data API

[![CI](https://github.com/timpugh/nba-data-api/actions/workflows/ci.yml/badge.svg)](https://github.com/timpugh/nba-data-api/actions/workflows/ci.yml)
[![Python 3.13](https://img.shields.io/badge/python-3.13-blue.svg)](https://www.python.org/downloads/)
[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)

A serverless REST API and Apple-aesthetic search frontend for NBA per-season
player stats (1996–97 → 2022–23). Type a name, get the player's full career
line — every season, every team. Built on AWS Lambda, API Gateway, DynamoDB,
S3, and CloudFront, deployed via the AWS CDK.

**Live demo:** [https://d222u5c44dorfm.cloudfront.net/](https://d222u5c44dorfm.cloudfront.net/)

---

## What's in this repo

| Path | Contents |
|---|---|
| [`lambda/`](lambda/) | API handler — Powertools-routed Lambda serving `GET /players` and `GET /players/{id}` |
| [`lambda_importer/`](lambda_importer/) | One-shot importer Lambda — parses the CSV and BatchWrites items to DynamoDB |
| [`hello_world/`](hello_world/) | CDK constructs and stacks (backend, frontend, WAF) |
| [`frontend/`](frontend/) | Static UI shipped to S3 → CloudFront |
| [`data/NBA_Player_Data.csv`](data/) | Source dataset (12,844 player-seasons) committed for reproducible builds |
| [`docs/dynamodb_schema.md`](docs/dynamodb_schema.md) | Schema design + access patterns — read before changing any key attribute |
| [`tests/`](tests/) | Unit, CDK assertion, and live-integration tests |
| [`CLAUDE.md`](CLAUDE.md) | Project conventions, encryption posture, cdk-nag gates |

## Architecture at a glance

```
                    ┌──────────────┐
                    │  CloudFront  │ ◄── static UI (S3, OAC, WAF)
                    └──────┬───────┘
                           │
                    ┌──────▼───────┐
                    │ API Gateway  │ ◄── /players  /players/{id}
                    └──────┬───────┘
                           │
                    ┌──────▼───────┐         ┌─────────────────┐
                    │   Lambda     │◄────────│  AppConfig      │ (feature flags)
                    │ (Powertools) │◄────────│  SSM Parameter  │ (greeting)
                    └──────┬───────┘
                           │
                    ┌──────▼───────────┐        ┌────────────────────┐
                    │  DynamoDB        │◄───────│  Importer Lambda   │◄── CSV asset (S3)
                    │  (CMK encrypted) │        │  (CR-triggered)    │
                    │  + 2 GSIs        │        └────────────────────┘
                    └──────────────────┘
```

Three CDK stacks: **WAF** (always `us-east-1`), **backend**, **frontend**.
The backend stack owns the table, both Lambdas, and the importer trigger. The
importer runs on stack create and re-fires automatically when the CSV or the
importer's code changes. Schema: [docs/dynamodb_schema.md](docs/dynamodb_schema.md).

## Prerequisites

| Tool | Why |
|---|---|
| Python 3.13 | Lambda runtime + local environments |
| [`uv`](https://docs.astral.sh/uv/) | Dependency resolver (`brew install uv` or `pipx install uv`) |
| [AWS CDK CLI](https://docs.aws.amazon.com/cdk/v2/guide/cli.html) | `npm install -g aws-cdk` |
| Docker | Required by CDK's PythonFunction bundler |
| AWS credentials | Configured for the deploying account (`aws configure` or SSO) |
| Node.js | For the CDK CLI |

## Setup

```bash
git clone https://github.com/timpugh/nba-data-api.git
cd nba-data-api
make install        # installs both venvs + pre-commit hooks
make doctor         # confirms uv, cdk, drawio, venv state, hook wiring
```

`make install` creates **two** project-local virtualenvs because CDK and
Powertools pin incompatible `attrs` versions:

| Venv | Purpose | Activated by |
|---|---|---|
| `.venv` | CDK workstation (synth, deploy, stack tests) | Default for any `make` target |
| `.venv-lambda` | Lambda runtime (unit tests, importer, OpenAPI gen) | Targets set `UV_PROJECT_ENVIRONMENT=.venv-lambda` |

Both live at the repo root, are gitignored, and never need manual activation —
the Makefile picks the right one. The conflict resolutions live in a single
`uv.lock` via `[tool.uv.conflicts]`.

If something gets weird: `make clean-venvs && make install`.

## IDE setup (VS Code)

**Open the workspace file**, not the folder:

```
File → Open Workspace from File… → practice.code-workspace
```

The workspace declares four folder roots (`.`, `lambda/`, `tests/unit/`,
`scripts/`) each pinned to the correct interpreter. The effect:

- **Pylance** resolves `aws_cdk` against `.venv` and `aws_lambda_powertools`
  against `.venv-lambda` simultaneously — no red squiggles on either side.
- **Test Explorer** runs unit tests under `.venv-lambda` and CDK stack tests
  under `.venv` independently.
- **Terminals** opened from each root auto-activate that root's venv.

Opening the folder directly (instead of the workspace) works for the CDK
side but Powertools imports in `lambda/` show as unresolved. Use the
workspace file.

`F5` debug configs are pre-wired in `.vscode/launch.json` — current file,
pytest on current file, and CDK synth under debugpy.

## Build, test, and develop

| Command | What it does | Venv |
|---|---|---|
| `make test` | Unit tests with 100% coverage gate | `.venv-lambda` |
| `make test-cdk` | CDK stack assertion tests (cdk-nag rule packs) | `.venv` |
| `make test-integration` | Live tests against a deployed stack | `.venv-lambda` |
| `make lint` | All pre-commit hooks (ruff, mypy, pylint, bandit, xenon, pip-audit) | both |
| `make format` | ruff format | both |
| `make typecheck` | mypy on both sides | both |
| `make cdk-synth` | Synthesize all stacks with cdk-nag enforcement | `.venv` |

**`make test-cdk` synthesizes via `Template.from_stack()`, which does NOT raise on
cdk-nag findings** — only the CLI synth does. Always run `make cdk-synth`
before pushing CDK changes (requires Docker). CI catches this too; locally is
faster.

Coverage is gated at 100% — new lambda code without tests will fail.

## Deploy

**One-time setup** (per AWS account/region):

```bash
cdk bootstrap aws://<account>/us-east-1
```

**Deploy everything:**

```bash
make deploy           # → cdk deploy '**' --require-approval never (us-east-1)
```

CDK outputs surface the URLs you need:

- `CloudFrontDomainName` — public frontend
- `HelloWorldApiOutput` — API Gateway base
- `NbaPlayerTableName` — DynamoDB table

On a fresh deploy the importer Lambda fires once via the custom-resource
trigger and writes ~15,500 items. The trigger re-runs automatically on
subsequent deploys when the CSV asset hash OR the importer code asset hash
changes — no manual `aws lambda invoke`.

**Deploying to a different region:**

```bash
cdk deploy '**' -c region=us-west-2
```

The WAF stack always lands in `us-east-1` (CloudFront requirement); CDK
bridges the WebACL ARN cross-region via SSM Parameter Store automatically.

**Tear down:**

```bash
make destroy          # cdk destroy '**' (interactive)
```

The CSV asset bucket and CloudFront access logs are retained by design for
audit — delete them manually if you want a fully clean slate.

## Project conventions

- **Conventional commits** drive the changelog: `feat:`, `fix:`, `docs:`, `chore:`, `ci:`, `test:`, `refactor:`, `build:`. Regenerate with `git cliff -o CHANGELOG.md`.
- **No `Co-Authored-By:` trailer** on commits.
- **Pre-commit hooks** run on every commit — don't `--no-verify`; fix the issue.
- **Encryption posture** is non-negotiable: every data-bearing resource that supports a per-resource CMK uses the project CMK. See [CLAUDE.md](CLAUDE.md) for the full posture and confused-deputy-grant patterns.
- **cdk-nag is a hard gate.** Five rule packs run on every synth (AwsSolutions, Serverless, NIST 800-53 R5, HIPAA Security, PCI DSS 3.2.1). Suppressions need a real `reason=` — "not needed" gets rejected in review.

## Common dev workflows

Add a new API route:

1. Define a Pydantic model + `@app.get(...)` handler in [`lambda/app.py`](lambda/app.py).
2. Wire the API Gateway resource + method + CORS preflight in [`hello_world/hello_world_app.py`](hello_world/hello_world_app.py).
3. Add unit tests in [`tests/unit/test_handler.py`](tests/unit/test_handler.py) (coverage gate is 100%).
4. Add an integration smoke test in [`tests/integration/test_api_gateway.py`](tests/integration/test_api_gateway.py).
5. `make test && make test-cdk && make cdk-synth`.

Refresh the dataset:

1. Replace [`data/NBA_Player_Data.csv`](data/) with the new content.
2. `make deploy` — the CR trigger sees the new asset hash and re-imports automatically.

Add a new DynamoDB access pattern:

1. Read [`docs/dynamodb_schema.md`](docs/dynamodb_schema.md) first.
2. If it fits an existing GSI, just add the query. If not, add a third GSI rather than fragmenting an existing one.

## Documentation

- [`docs/dynamodb_schema.md`](docs/dynamodb_schema.md) — single-table design, GSIs, access patterns, import contract
- [`CLAUDE.md`](CLAUDE.md) — project conventions, encryption posture, two-venv split rationale
- [`TODO.md`](TODO.md) — production-readiness gates (auth, request validation, backups, custom domain)
- [`CHANGELOG.md`](CHANGELOG.md) — release history

API reference (OpenAPI spec rendered with Scalar) is published via GitHub
Pages on docs deploy: `make docs-serve` for local preview with hot reload.

## Acknowledgments

Built on the
[lambda-powertools-reference](https://github.com/timpugh/lambda-powertools-reference)
template, which contributes the CDK scaffolding, encryption posture, cdk-nag
gates, observability stack, two-venv pattern, and CI/CD. The NBA-specific
work — DynamoDB schema, importer, API routes, search UI — layers on top.

## License

[Apache 2.0](LICENSE).
