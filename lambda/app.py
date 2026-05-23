"""Hello World Lambda function using AWS Lambda Powertools.

This module implements a serverless API endpoint that returns a greeting message.
It demonstrates the use of Powertools utilities including structured logging,
X-Ray tracing, CloudWatch metrics, idempotency, SSM parameters, feature flags,
Pydantic-backed request/response validation (with an OpenAPI spec generated
at documentation-build time — see scripts/generate_openapi.py), and Event Source
Data Classes.
"""

import os
from decimal import Decimal
from typing import Any, cast

import boto3
from aws_lambda_powertools import Logger, Metrics, Tracer
from aws_lambda_powertools.event_handler import APIGatewayRestResolver
from aws_lambda_powertools.event_handler.api_gateway import CORSConfig
from aws_lambda_powertools.event_handler.exceptions import (
    InternalServerError,
    NotFoundError,
)
from aws_lambda_powertools.metrics import MetricUnit
from aws_lambda_powertools.utilities.data_classes import APIGatewayProxyEvent
from aws_lambda_powertools.utilities.feature_flags import AppConfigStore, FeatureFlags
from aws_lambda_powertools.utilities.feature_flags.exceptions import (
    ConfigurationStoreError,
    SchemaValidationError,
    StoreClientError,
)
from aws_lambda_powertools.utilities.idempotency import (
    DynamoDBPersistenceLayer,
    idempotent,
)
from aws_lambda_powertools.utilities.idempotency.config import IdempotencyConfig
from aws_lambda_powertools.utilities.idempotency.exceptions import IdempotencyKeyError
from aws_lambda_powertools.utilities.parameters import get_parameter
from aws_lambda_powertools.utilities.parameters.exceptions import GetParameterError
from aws_lambda_powertools.utilities.typing import LambdaContext
from pydantic import BaseModel, Field


def _require_env(name: str) -> str:
    """Return the env var or raise at import time with a clear message.

    A missing env var (table name, profile name, etc.) only surfaces deep
    inside boto3 as an opaque parameter-validation error. Failing here makes
    the misconfiguration obvious in CloudWatch on the very first invocation.
    """
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Required environment variable {name!r} is not set")
    return value


logger = Logger()
tracer = Tracer()
metrics = Metrics()

# enable_validation=True wires Pydantic into the resolver. Request bodies and
# response return types are validated against their model annotations, and
# those same models drive the OpenAPI schema read by scripts/generate_openapi.py.
# We deliberately do NOT call app.enable_swagger() here — exposing the spec at
# runtime would publish the full API surface to any caller. The spec is
# instead rendered into Zensical at documentation-build time.
app = APIGatewayRestResolver(
    # allow_headers is only relevant for the response-side CORS Access-Control-
    # Allow-Headers value, but for completeness we list Idempotency-Key here
    # too — keeps the Powertools CORSConfig in sync with API Gateway's preflight
    # configuration declared in CDK.
    cors=CORSConfig(
        allow_origin="*",
        max_age=300,
        allow_headers=[
            "Content-Type",
            "X-Amz-Date",
            "Authorization",
            "X-Api-Key",
            "X-Amzn-Trace-Id",
            "Idempotency-Key",
        ],
    ),
    enable_validation=True,
)

# Idempotency setup.
# Key on the client-supplied "Idempotency-Key" header (case-insensitive lookup
# matches both "Idempotency-Key" and "idempotency-key"). raise_on_no_idempotency_key
# turns a missing header into Powertools' IdempotencyKeyError, which the resolver
# below converts into a 400 BadRequest — making the requirement enforced rather
# than implicit. Keying on a client-controlled value (instead of the server-
# generated requestContext.requestId, which changes on every retry) is what
# actually makes the layer deduplicate.
persistence_layer = DynamoDBPersistenceLayer(
    table_name=_require_env("IDEMPOTENCY_TABLE_NAME"),
)
idempotency_config = IdempotencyConfig(
    event_key_jmespath='headers."Idempotency-Key" || headers."idempotency-key"',
    raise_on_no_idempotency_key=True,
    expires_after_seconds=3600,
)

# Feature Flags setup
app_config_store = AppConfigStore(
    environment=_require_env("APPCONFIG_ENV_NAME"),
    application=_require_env("APPCONFIG_APP_NAME"),
    name=_require_env("APPCONFIG_PROFILE_NAME"),
)
feature_flags = FeatureFlags(store=app_config_store)

# Greeting parameter name resolved at module load — fail loudly on
# misconfiguration rather than letting boto3 reject an empty key at runtime.
GREETING_PARAM_NAME = _require_env("GREETING_PARAM_NAME")

# NBA player table — schema documented in docs/dynamodb_schema.md.
# Reused across requests; boto3 client instantiation outside the handler keeps
# warm containers free of repeated connection setup.
NBA_TABLE_NAME = _require_env("NBA_TABLE_NAME")
_nba_table = boto3.resource("dynamodb").Table(NBA_TABLE_NAME)


class HelloResponse(BaseModel):
    """Response body for GET /hello."""

    message: str = Field(
        ...,
        description="Greeting from SSM Parameter Store, optionally suffixed when the enhanced_greeting flag is on.",
        examples=["hello world", "hello world - enhanced mode enabled"],
    )


class PlayerSeasonStats(BaseModel):
    """Per-season aggregate stats for one player.

    Mirrors the SEASON#<yyyy-yy> item layout — see docs/dynamodb_schema.md
    for the source CSV columns and storage keys.
    """

    season: str = Field(..., examples=["2022-23"])
    team_abbreviation: str = Field(..., examples=["HOU"])
    age: int | None = None
    player_height_cm: float | None = None
    player_weight_kg: float | None = None
    gp: int | None = Field(default=None, description="Games played")
    pts: float | None = Field(default=None, description="Points per game")
    reb: float | None = Field(default=None, description="Rebounds per game")
    ast: float | None = Field(default=None, description="Assists per game")
    net_rating: float | None = None
    oreb_pct: float | None = None
    dreb_pct: float | None = None
    usg_pct: float | None = None
    ts_pct: float | None = None
    ast_pct: float | None = None


class PlayerResponse(BaseModel):
    """Response body for GET /players/{id} — profile + every season on file."""

    player_id: str = Field(..., description="Stable UUIDv5 derived from name + draft data")
    player_name: str
    college: str | None = None
    country: str | None = None
    draft_year: int | None = None
    draft_round: int | None = None
    draft_number: int | None = None
    seasons: list[PlayerSeasonStats]


class PlayerSummary(BaseModel):
    """Lightweight player entry used by GET /players for client-side search."""

    player_id: str
    player_name: str
    draft_year: int | None = None


class PlayerListResponse(BaseModel):
    """Response body for GET /players — all profile rows in one payload.

    Returned in a single response (no pagination cursor) because the dataset
    is bounded at ~2,551 players. If the catalog grows beyond a few thousand
    rows, switch to a cursor-based pattern and add an ``after`` query param.
    """

    players: list[PlayerSummary]
    count: int


@app.get(
    "/hello",
    summary="Return a greeting",
    description=(
        "Returns the greeting string configured in SSM Parameter Store. "
        "When the `enhanced_greeting` AppConfig feature flag is enabled for "
        "the caller's source IP, the response includes the feature flag's "
        "configured suffix."
    ),
    response_description="A JSON object containing the resolved greeting.",
    tags=["Greeting"],
)
@tracer.capture_method
def hello() -> HelloResponse:
    """Handle GET /hello requests.

    Fetches the greeting from SSM Parameter Store, checks the enhanced_greeting
    feature flag, emits a CloudWatch metric, and logs request metadata from
    the API Gateway event.

    Returns:
        HelloResponse: Validated response model with a ``message`` field.
    """
    metrics.add_metric(name="HelloRequests", unit=MetricUnit.Count, value=1)

    # Access typed event data via Event Source Data Classes
    event: APIGatewayProxyEvent = app.current_event
    source_ip = event.request_context.identity.source_ip
    user_agent = event.request_context.identity.user_agent
    request_id = event.request_context.request_id

    logger.info(
        "Request received",
        source_ip=source_ip,
        user_agent=user_agent,
        request_id=request_id,
    )

    # Fetch greeting from SSM Parameter Store. Powertools wraps boto3 errors
    # (ClientError, BotoCoreError) as GetParameterError; catch only that so
    # truly unexpected exceptions propagate to Powertools' default handler
    # and surface with the right type in metrics and X-Ray.
    # max_age=300 raises Powertools' in-memory TTL from its 5-second default
    # so warm containers reuse the value for 5 minutes between SSM calls.
    # The greeting changes via deployment, not at runtime, so a longer TTL
    # is safe and meaningfully reduces SSM API spend at higher RPS.
    try:
        greeting = get_parameter(GREETING_PARAM_NAME, max_age=300)
    except GetParameterError as exc:
        logger.exception("Failed to fetch greeting from SSM", param_name=GREETING_PARAM_NAME)
        raise InternalServerError("Failed to fetch greeting") from exc
    logger.info("Greeting fetched from parameter store", greeting=greeting)

    # Check feature flag — non-critical, fall back to default on failure.
    # Pass source_ip + user_agent as context so AppConfig rules can match on
    # them (the route's docstring promises IP-based gating; without context
    # the rule engine can never see the values to evaluate against).
    # Catch only the Powertools FeatureFlags exception types — programming
    # errors (TypeError, AttributeError) intentionally propagate so they
    # surface as bugs in metrics rather than being silently absorbed by the
    # fallback path.
    try:
        enhanced = feature_flags.evaluate(
            name="enhanced_greeting",
            context={"source_ip": source_ip, "user_agent": user_agent},
            default=False,
        )
    except (ConfigurationStoreError, SchemaValidationError, StoreClientError):
        logger.warning("Feature flag evaluation failed, falling back to default")
        enhanced = False

    if enhanced:
        message = f"{greeting} - enhanced mode enabled"
        logger.info("Enhanced greeting enabled")
    else:
        message = greeting

    return HelloResponse(message=message)


def _to_float(value: Any) -> float | None:
    """Coerce a DynamoDB numeric (Decimal) attribute into a float, or pass through None.

    DDB returns Decimal for every Number attribute; Pydantic accepts Decimal
    for ``float`` fields but the conversion clutters response payloads and
    interacts poorly with JSON serialization at the API Gateway edge. Casting
    here keeps the wire format consistent.
    """
    if value is None:
        return None
    if isinstance(value, Decimal):
        return float(value)
    return float(value)


@app.get(
    "/players",
    summary="List every player in the dataset",
    description=(
        "Returns a lightweight summary (id, name, draft year) for every "
        "player profile. Used by the frontend to populate a client-side "
        "search index — a single round-trip avoids per-keystroke API calls. "
        "Paginates internally through GSI2 partition ``PLAYERS#ALL`` so the "
        "full set comes back regardless of DynamoDB's 1 MB Query page limit."
    ),
    response_description="All players sorted alphabetically by lowercased name.",
    tags=["Players"],
)
@tracer.capture_method
def list_players() -> PlayerListResponse:
    """Handle GET /players."""
    metrics.add_metric(name="ListPlayersRequests", unit=MetricUnit.Count, value=1)

    players: list[PlayerSummary] = []
    last_key: dict[str, Any] | None = None
    try:
        while True:
            kwargs: dict[str, Any] = {
                "IndexName": "gsi2-by-season",
                "KeyConditionExpression": "GSI2PK = :pk",
                "ExpressionAttributeValues": {":pk": "PLAYERS#ALL"},
                "ProjectionExpression": "player_id, player_name, draft_year",
            }
            if last_key:
                kwargs["ExclusiveStartKey"] = last_key
            page = _nba_table.query(**kwargs)
            for item in page.get("Items", []):
                players.append(
                    PlayerSummary(
                        player_id=item["player_id"],
                        player_name=item["player_name"],
                        draft_year=item.get("draft_year"),
                    )
                )
            last_key = page.get("LastEvaluatedKey")
            if not last_key:
                break
    except Exception as exc:
        logger.exception("DynamoDB query failed for list_players")
        raise InternalServerError("Failed to list players") from exc

    return PlayerListResponse(players=players, count=len(players))


@app.get(
    "/players/<player_id>",
    summary="Get a player's profile and full season history",
    description=(
        "Returns the stable profile (name, college, draft info) plus every "
        "season the player appears in the dataset, ordered oldest-first. "
        "Returns 404 when no profile row exists for the given ID."
    ),
    response_description="Player profile with embedded list of season stats.",
    tags=["Players"],
)
@tracer.capture_method
def get_player(player_id: str) -> PlayerResponse:
    """Handle GET /players/{id}.

    One Query against the base table partition ``PLAYER#<id>`` returns the
    profile row plus every season row in a single round-trip. Cheaper than
    a separate GetItem(profile) + Query(seasons), and the items already sort
    naturally (``PROFILE`` < ``SEASON#...``).
    """
    metrics.add_metric(name="GetPlayerRequests", unit=MetricUnit.Count, value=1)
    logger.info("get_player requested", player_id=player_id)

    pk = f"PLAYER#{player_id}"
    try:
        result = _nba_table.query(
            KeyConditionExpression="PK = :pk",
            ExpressionAttributeValues={":pk": pk},
        )
    except Exception as exc:
        logger.exception("DynamoDB query failed", player_id=player_id)
        raise InternalServerError("Failed to fetch player") from exc

    items: list[dict[str, Any]] = result.get("Items", [])
    profile_row: dict[str, Any] | None = None
    season_rows: list[dict[str, Any]] = []
    for item in items:
        if item.get("SK") == "PROFILE":
            profile_row = item
        else:
            season_rows.append(item)

    if profile_row is None:
        raise NotFoundError

    season_rows.sort(key=lambda r: r["SK"])

    return PlayerResponse(
        player_id=profile_row["player_id"],
        player_name=profile_row["player_name"],
        college=profile_row.get("college") or None,
        country=profile_row.get("country") or None,
        draft_year=profile_row.get("draft_year"),
        draft_round=profile_row.get("draft_round"),
        draft_number=profile_row.get("draft_number"),
        seasons=[
            PlayerSeasonStats(
                season=row["season"],
                team_abbreviation=row["team_abbreviation"],
                age=row.get("age"),
                player_height_cm=_to_float(row.get("player_height_cm")),
                player_weight_kg=_to_float(row.get("player_weight_kg")),
                gp=row.get("gp"),
                pts=_to_float(row.get("pts")),
                reb=_to_float(row.get("reb")),
                ast=_to_float(row.get("ast")),
                net_rating=_to_float(row.get("net_rating")),
                oreb_pct=_to_float(row.get("oreb_pct")),
                dreb_pct=_to_float(row.get("dreb_pct")),
                usg_pct=_to_float(row.get("usg_pct")),
                ts_pct=_to_float(row.get("ts_pct")),
                ast_pct=_to_float(row.get("ast_pct")),
            )
            for row in season_rows
        ],
    )


@idempotent(config=idempotency_config, persistence_store=persistence_layer)
def _resolve_with_idempotency(event: dict[str, Any], context: LambdaContext) -> dict[str, Any]:
    """Inner handler wrapped by @idempotent.

    Split out so the outer handler can catch IdempotencyKeyError (raised by
    @idempotent before this body runs when the request has no Idempotency-Key
    header) and return a 400 instead of letting Lambda surface a 500.
    """
    return cast("dict[str, Any]", app.resolve(event, context))


@logger.inject_lambda_context
@tracer.capture_lambda_handler
@metrics.log_metrics(capture_cold_start_metric=True)
def lambda_handler(event: dict[str, Any], context: LambdaContext) -> dict[str, Any]:
    """Lambda entry point.

    Resolves the API Gateway event through the router and returns the result.
    Decorated with Powertools Logger, Tracer, Metrics; the inner function
    handles Idempotency so a missing Idempotency-Key header surfaces as a 400
    instead of an unhandled 500.

    Args:
        event: API Gateway Lambda proxy event.
        context: Lambda runtime context.

    Returns:
        dict: API Gateway Lambda proxy response.
    """
    # cast() restores the return type after @idempotent erases it. Powertools'
    # app.resolve() is well-typed in .venv-lambda, but the @idempotent wrapper
    # passes return values through as Any; .venv (CDK side, no Powertools)
    # already sees the function as Any. The cast is a no-op at runtime.
    try:
        return cast(dict, _resolve_with_idempotency(event, context))
    except IdempotencyKeyError:
        logger.warning("Request rejected: missing Idempotency-Key header")
        return {
            "statusCode": 400,
            "headers": {"Content-Type": "application/json"},
            "body": '{"message":"Idempotency-Key header is required"}',
            "isBase64Encoded": False,
        }
