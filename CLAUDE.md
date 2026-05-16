# CLAUDE.md

Project-level instructions for future Claude Code sessions. Loaded on every session against this repo.

## Project

Serverless REST API exposing NBA player data. Built on the [lambda-powertools-reference](https://github.com/timpugh/lambda-powertools-reference) template, which contributes the CDK scaffolding, encryption posture, observability stack, CI/CD, and supply-chain hygiene. The application code is being layered on top.

## Environments — two venvs, never mix

CDK and Powertools require incompatible `attrs` versions (CDK pulls `attrs<26` via jsii; Powertools pulls `attrs>=26`). The project declares them as conflicts in `[tool.uv.conflicts]` so one `uv.lock` holds both resolutions:

- `.venv` — CDK workstation. Used for `cdk synth`, `cdk deploy`, stack-assertion tests, lint/format/typecheck of `hello_world/` (rename pending).
- `.venv-lambda` — Lambda runtime. Used for unit tests over `lambda/`, integration tests, the OpenAPI generator script.

`make install` provisions both. The Makefile uses `UV_PROJECT_ENVIRONMENT=.venv-lambda` to switch into the runtime env without an activation dance. Never install Powertools into `.venv` or CDK into `.venv-lambda`.

Run `make doctor` after `make install` to confirm both venvs picked up the expected groups, `cdk`/`drawio` are on `PATH`, and pre-commit is wired into `.git/hooks/`. If a venv gets corrupted: `make clean-venvs && make install`.

## CDK synth must use `'**'`

All three stacks live inside `HelloWorldStage` (a `cdk.Stage` — to be renamed). Bare `cdk synth` walks only the App's direct children, finds the Stage, doesn't recurse, and emits an empty synthesis that succeeds *without* running cdk-nag against the real stacks. `make cdk-synth` and the CI `cdk-check` job both invoke `cdk synth '**'` for this reason. If you run `cdk synth` directly during development, include the glob — otherwise the gate passes silently regardless of what cdk-nag would find.

## cdk-nag is a hard gate AND must run via CLI synth, not assertion tests

Five rule packs run on every synth: AwsSolutions, Serverless, NIST 800-53 R5, HIPAA Security, PCI DSS 3.2.1. Findings fail CI. Resolve by:

1. **Fix the underlying issue** (preferred). The template README's "Design decisions and known limitations" section documents recurring patterns.
2. **Suppress with rationale**. Every suppression carries a `reason=` string. Boilerplate suppressions like `"not needed"` get rejected in review. For `AwsSolutions-IAM5` wildcards, scope with `applies_to=["Resource::*"]` or a specific pattern and explain *why* the wildcard is unavoidable.

**Critical local-vs-CI gap**: `make test-cdk` uses `Template.from_stack()`, which synthesizes the stack but does **NOT** raise on cdk-nag Aspect errors. The CI `cdk-check` job runs `cdk synth '**'` via the CLI which does. So passing local tests is necessary but not sufficient — a clean local run can still ship a cdk-nag-failing commit. Either start Docker and run `make cdk-synth` locally before pushing, or expect to iterate against CI when adding constructs that touch IAM policies. Suppressions labeled `"... not needed for sample app"` in `hello_world/hello_world_stack.py` are real production gates that need addressing before customer traffic — see the Production readiness checklist in `TODO.md`.

## Encryption posture

Every data-bearing resource that supports a per-resource customer-managed key uses the project's CMK: DynamoDB, Lambda env vars, all log groups, the frontend S3 bucket, AppConfig hosted configuration content, SQS DLQs, and CloudTrail trail log files (per-object SSE-KMS into an SSE-S3 bucket). Account/region-wide encryption settings (X-Ray, Glue Data Catalog) are deliberately out of scope — they'd mutate state shared with other apps in the account.

When adding new resources, check whether they support `encryption_key=` (or equivalent) and wire it. Service-principal grants on CMKs must be confused-deputy-guarded with `aws:SourceAccount` + `aws:SourceArn` — see `grant_logs_service_to_key` / `grant_guardduty_service_to_key` in `hello_world/nag_utils.py` (path renames pending) for the canonical pattern.

## Dangling resources

Services that create supporting resources outside CloudFormation (CloudWatch log groups, dashboards, etc.) don't get cleaned up by `cdk destroy`. The template ships two cleanup `AwsCustomResource` patterns to handle this:

- `AppInsightsDashboardCleanup` — deletes the auto-created AI dashboard
- `RumLogGroupCleanup` — deletes the auto-created `/aws/vendedlogs/RUMService_*` log group

When adding services that create supporting AWS resources outside CFN, mirror the pattern: a Lambda-backed `cr.AwsCustomResource` with an `on_delete` SDK call, scoped IAM, and `ignore_error_codes_matching="ResourceNotFoundException"` for the case where no events ever materialized.

## Conventional Commits + git-cliff drive `CHANGELOG.md`

Commit prefix grammar (see template README "Commit message convention"):

`feat:` / `fix:` / `docs:` / `chore:` / `ci:` / `test:` / `refactor:` / `build:`

The `cliff.toml` config maps these to Keep-a-Changelog groups. Regenerate with `git cliff -o CHANGELOG.md` after a release. Dependabot bumps (`build(deps):` / `chore(deps):` / bare `Bump X from Y to Z`) and `Merge pull request` commits are filtered out by design.

## Releases

Driven from conventional-commit history. Recipe in template README "Cutting a release". One-line summary:

```bash
git cliff --bumped-version    # ask git-cliff what version to use
# update pyproject.toml, run make lock
git cliff --tag vX.Y.Z -o CHANGELOG.md
git commit -m "chore: release vX.Y.Z"
git tag -a vX.Y.Z -m "..."
git push origin main && git push origin vX.Y.Z
gh release create vX.Y.Z --notes-from-tag --latest --verify-tag
```

## Behaviors to avoid

- **No `Co-Authored-By:` trailer on commits.** Personal preference.
- **Don't introduce account/region-wide CDK constructs** without flagging them explicitly. `glue.CfnDataCatalogEncryptionSettings`, `xray.UpdateEncryptionConfig`, and similar mutate state shared with other apps in the deploying account. Forks dropping this stack into an existing AWS account would silently override neighbor teams' settings.
- **Don't commit `cdk.out/`, `report.html`, `htmlcov/`, `.coverage`, or `site/`.** Reproducible from source; large; gitignored already — but worth knowing if a CI failure surfaces stale artifacts locally.

## Post-template setup (one-time per new repo, already done for this repo)

When this repo was spawned from the template, two one-time setup steps were required that the template can't carry:

1. **GitHub Pages enabled** via `gh api repos/<owner>/<repo>/pages -X POST -f build_type=workflow`. Requires the repo to be public on the free plan. Without it the Docs workflow returns HTTP 404.
2. **CDK bootstrap** in the target AWS account+region — `cdk bootstrap aws://<account>/us-east-1` — needed before the first `cdk deploy`.

Both are documented here for any future re-spawn from the template.

## NBA-specific guidance (planned, not yet implemented)

- **Upstream client** lives in `lambda/nba.py` (planned). Wraps whichever NBA data source we land on (`nba_api` PyPI package, balldontlie REST, or stats.nba.com directly). NBA Stats API rate-limits aggressively and rejects unauthenticated requests from certain user agents — the wrapper handles backoff + UA rotation.
- **Caching is mandatory, not optional.** Reuse the existing DynamoDB idempotency-table pattern, or add a dedicated `NbaResponseCache` table with TTL keyed on `(endpoint, params)`. Time-windowed cache (24h for player metadata, 5min for live scores).
- **API key (if needed) goes in SSM Parameter Store as SecureString** via the AwsCustomResource workaround (CFN doesn't support SecureString natively). Read via Powertools' `get_parameter` with `max_age=300`.
- **Feature flags via AppConfig** — good fit for "use NBA Stats API v2 endpoints", "enable advanced stats", "include playoff games".
- **Routes mirror NBA Stats API shape**: `GET /players`, `GET /players/{id}`, `GET /teams/{id}/roster`, `GET /games/{date}`, `GET /stats/{season}/players/{id}`. Add Pydantic models per route so the build-time OpenAPI spec stays meaningful.
