"""NBA player-data importer Lambda.

Triggered by a CDK custom resource on stack create/update when the CSV
asset hash changes. Downloads the CSV from S3, derives a deterministic
player_id per row, and writes one PROFILE item per distinct player plus
one SEASON#<yyyy-yy> item per row into the NBA DynamoDB table.

Idempotent: re-runs overwrite the same items because player_id is
UUIDv5 of (name, college, draft_year, draft_number) and SK is fixed.

The schema is documented in ``docs/dynamodb_schema.md``.
"""

from __future__ import annotations

import csv
import io
import logging
import os
import uuid
from decimal import Decimal
from typing import Any

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Stable namespace for UUIDv5 derivation. Picked once and frozen forever —
# changing it would mint new player_ids and break existing items.
_NAMESPACE = uuid.UUID("e7c9a3b2-5d4f-4a1e-8c6b-9f2d3e1a0b7c")

# Sentinel strings the source CSV uses to indicate "no value": draft_* fields
# carry "Undrafted" for undrafted players, college carries "None" for players
# without a college (e.g. straight-to-NBA prospects).
_NULL_MARKERS = frozenset(("", "None", "Undrafted"))


def _decimal(value: str) -> Decimal | None:
    """Parse a CSV cell to Decimal, returning None for null-marker strings.

    DynamoDB has no float type — numbers go in as Decimal.
    """
    if value is None or value in _NULL_MARKERS:
        return None
    return Decimal(value)


def _int(value: str) -> int | None:
    if value is None or value in _NULL_MARKERS:
        return None
    return int(value)


def _str(value: str | None) -> str | None:
    """Normalize string sentinels to None.

    The source CSV carries "None" for missing college (e.g. LeBron James went
    straight from high school) and uses the same _NULL_MARKERS vocabulary as
    numeric columns. Mapping these to None at the import boundary keeps the
    API response clean — callers see ``college: null`` instead of a literal
    "None" they have to special-case downstream.
    """
    if value is None or value in _NULL_MARKERS:
        return None
    return value


def _player_id(name: str, college: str, draft_year: str, draft_number: str) -> str:
    """Deterministic player ID disambiguating same-name players.

    Two real players named "Marcus Williams" exist in the dataset (one
    from Connecticut drafted 2006, one from Arizona drafted 2007). The
    composite key separates them; UUIDv5 makes the result stable across
    reimports.
    """
    seed = f"{name}|{college}|{draft_year}|{draft_number}"
    return str(uuid.uuid5(_NAMESPACE, seed))


def _build_items(rows: list[dict[str, str]]) -> list[dict[str, Any]]:
    """Produce DynamoDB items from CSV rows.

    Returns a flat list of profile + season items ready for BatchWriteItem.
    A collision check on player_id guards against schema-breaking dataset
    changes — if it ever fires, the dataset needs a wider composite key.
    """
    profiles: dict[str, dict[str, Any]] = {}
    season_items: list[dict[str, Any]] = []

    for row in rows:
        pid = _player_id(
            row["player_name"],
            row["college"],
            row["draft_year"],
            row["draft_number"],
        )

        profile = profiles.setdefault(
            pid,
            {
                "PK": f"PLAYER#{pid}",
                "SK": "PROFILE",
                "player_id": pid,
                "player_name": row["player_name"],
                "college": _str(row["college"]),
                "country": _str(row["country"]),
                "draft_year": _int(row["draft_year"]),
                "draft_round": _int(row["draft_round"]),
                "draft_number": _int(row["draft_number"]),
                "GSI2PK": "PLAYERS#ALL",
                "GSI2SK": f"NAME#{row['player_name'].lower()}#{pid}",
            },
        )
        # Sanity check: a stable player_id should not see conflicting bio data.
        # If it does, the composite key (name, college, draft_year, draft_number)
        # has collided across real-world players and the schema needs widening.
        if profile["player_name"] != row["player_name"]:
            raise RuntimeError(f"player_id collision detected for {pid}: {profile} vs {row}")

        season = row["season"]
        season_items.append(
            {
                "PK": f"PLAYER#{pid}",
                "SK": f"SEASON#{season}",
                "season": season,
                "team_abbreviation": row["team_abbreviation"],
                "age": _int(row["age"]),
                "player_height_cm": _decimal(row["player_height"]),
                "player_weight_kg": _decimal(row["player_weight"]),
                "gp": _int(row["gp"]),
                "pts": _decimal(row["pts"]),
                "reb": _decimal(row["reb"]),
                "ast": _decimal(row["ast"]),
                "net_rating": _decimal(row["net_rating"]),
                "oreb_pct": _decimal(row["oreb_pct"]),
                "dreb_pct": _decimal(row["dreb_pct"]),
                "usg_pct": _decimal(row["usg_pct"]),
                "ts_pct": _decimal(row["ts_pct"]),
                "ast_pct": _decimal(row["ast_pct"]),
                "GSI1PK": f"TEAM#{row['team_abbreviation']}#SEASON#{season}",
                "GSI1SK": f"PLAYER#{pid}",
                "GSI2PK": f"SEASON#{season}",
                "GSI2SK": f"PLAYER#{pid}",
            }
        )

    # Drop None-valued attributes — DynamoDB rejects them and they take up
    # storage we don't need. Sparse attributes (undrafted -> draft_* = None)
    # naturally become absent in the item.
    items = [*profiles.values(), *season_items]
    return [{k: v for k, v in item.items() if v is not None} for item in items]


def _batch_write(table: Any, items: list[dict[str, Any]]) -> None:
    """Write items via DynamoDB BatchWriter (auto-handles 25-item batches + retries)."""
    with table.batch_writer() as batch:
        for item in items:
            batch.put_item(Item=item)


def _load_csv(s3: Any, bucket: str, key: str) -> list[dict[str, str]]:
    body = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
    text = body.decode("utf-8")
    reader = csv.DictReader(io.StringIO(text))
    return list(reader)


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """CloudFormation custom-resource lifecycle handler.

    On Create/Update: download CSV, build items, BatchWriteItem.
    On Delete: no-op — the table is destroyed by CFN, individual deletes
    here would only slow the teardown and risk partial state if throttled.
    """
    request_type = event.get("RequestType", "Create")
    logger.info("Custom resource invoked: %s", request_type)

    if request_type == "Delete":
        return {"PhysicalResourceId": event.get("PhysicalResourceId", "nba-importer"), "Data": {}}

    bucket = os.environ["CSV_S3_BUCKET"]
    key = os.environ["CSV_S3_KEY"]
    table_name = os.environ["NBA_TABLE_NAME"]

    s3 = boto3.client("s3")
    table = boto3.resource("dynamodb").Table(table_name)

    rows = _load_csv(s3, bucket, key)
    logger.info("Parsed %d rows from s3://%s/%s", len(rows), bucket, key)

    items = _build_items(rows)
    logger.info("Built %d DynamoDB items (profiles + seasons)", len(items))

    _batch_write(table, items)
    logger.info("BatchWrite complete for %d items", len(items))

    return {
        "PhysicalResourceId": "nba-importer",
        "Data": {
            "RowsProcessed": str(len(rows)),
            "ItemsWritten": str(len(items)),
        },
    }
