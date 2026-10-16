import json
import math
import os
import re
import sqlite3
import sys
import zipfile
from datetime import date, datetime
from xml.etree.ElementTree import ParseError
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse


ROOT = Path(__file__).resolve().parent
STATIC_DIR = ROOT / "static"
DB_PATH = Path(os.environ.get("EWI_DB_PATH", ROOT / "instance" / "ewi.sqlite3"))
PORT = int(os.environ.get("EWI_PORT", "8001"))
MOVEMENT_TYPES = {"collection", "pickup", "delivery", "use", "return", "transfer"}
LOCATION_TYPES = {"warehouse", "team", "house"}


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def connect_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    return db


def initialize_db():
    with connect_db() as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS materials (
                id INTEGER PRIMARY KEY,
                code TEXT,
                name TEXT NOT NULL COLLATE NOCASE UNIQUE,
                unit TEXT NOT NULL DEFAULT 'units',
                opening_qty REAL
            );
            CREATE TABLE IF NOT EXISTS houses (
                id TEXT PRIMARY KEY,
                postcode TEXT NOT NULL DEFAULT '',
                owner_name TEXT NOT NULL DEFAULT '',
                employee_name TEXT NOT NULL DEFAULT '',
                is_finished INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS movements (
                id INTEGER PRIMARY KEY,
                happened_on TEXT NOT NULL,
                kind TEXT NOT NULL,
                material_id INTEGER NOT NULL REFERENCES materials(id),
                qty REAL NOT NULL CHECK (qty > 0),
                source TEXT NOT NULL,
                destination TEXT NOT NULL,
                house_id TEXT REFERENCES houses(id),
                delivery_number INTEGER,
                person TEXT NOT NULL DEFAULT '',
                team TEXT NOT NULL DEFAULT '',
                department TEXT NOT NULL DEFAULT '',
                notes TEXT NOT NULL DEFAULT '',
                FOREIGN KEY (material_id) REFERENCES materials(id)
            );
            CREATE INDEX IF NOT EXISTS movements_material_idx ON movements(material_id, id);
            CREATE TABLE IF NOT EXISTS stocktakes (
                id INTEGER PRIMARY KEY,
                material_id INTEGER NOT NULL REFERENCES materials(id),
                location TEXT NOT NULL,
                quantity REAL NOT NULL CHECK (quantity >= 0),
                movement_id INTEGER NOT NULL,
                counted_on TEXT NOT NULL,
                counted_by TEXT NOT NULL DEFAULT '',
                notes TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS stocktakes_location_idx
                ON stocktakes(material_id, location, id);
            CREATE TABLE IF NOT EXISTS app_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """
        )
        house_columns = {
            row["name"] for row in db.execute("PRAGMA table_info(houses)")
        }
        if "is_finished" not in house_columns:
            db.execute(
                "ALTER TABLE houses ADD COLUMN is_finished INTEGER NOT NULL DEFAULT 0"
            )
        movement_columns = {
            row["name"] for row in db.execute("PRAGMA table_info(movements)")
        }
        if "house_id" not in movement_columns:
            db.execute("ALTER TABLE movements ADD COLUMN house_id TEXT REFERENCES houses(id)")
            db.execute(
                """
                UPDATE movements
                SET house_id = CASE
                    WHEN destination LIKE 'house:%' THEN substr(destination, 7)
                    WHEN source LIKE 'house:%' THEN substr(source, 7)
                    ELSE NULL
                END
                WHERE house_id IS NULL
                """
            )
        if "delivery_number" not in movement_columns:
            db.execute("ALTER TABLE movements ADD COLUMN delivery_number INTEGER")
        delivery_rows = db.execute(
            "SELECT id, house_id FROM movements "
            "WHERE kind = 'delivery' AND delivery_number IS NULL ORDER BY id"
        ).fetchall()
        delivery_numbers = {
            row["house_id"]: row["number"]
            for row in db.execute(
                """
                SELECT house_id, MAX(delivery_number) AS number
                FROM movements
                WHERE kind = 'delivery' AND delivery_number IS NOT NULL
                GROUP BY house_id
                """
            )
        }
        for row in delivery_rows:
            if not row["house_id"]:
                continue
            number = delivery_numbers.get(row["house_id"], 0) + 1
            delivery_numbers[row["house_id"]] = number
            db.execute(
                "UPDATE movements SET delivery_number = ? WHERE id = ?",
                (number, row["id"]),
            )
        house_columns = {row["name"] for row in db.execute("PRAGMA table_info(houses)")}
        if "employee_name" not in house_columns:
            db.execute(
                "ALTER TABLE houses ADD COLUMN employee_name TEXT NOT NULL DEFAULT ''"
            )
        if "department" not in movement_columns:
            db.execute(
                "ALTER TABLE movements ADD COLUMN department TEXT NOT NULL DEFAULT ''"
            )
        db.execute("CREATE INDEX IF NOT EXISTS movements_house_idx ON movements(house_id)")
        db.execute(
            "CREATE INDEX IF NOT EXISTS movements_delivery_idx ON movements(house_id, delivery_number)"
        )


def location_key(kind, reference=""):
    if kind == "warehouse":
        return "warehouse"
    if kind == "team":
        return team_location(reference)
    if kind not in LOCATION_TYPES or not reference or ":" in reference:
        raise ApiError(400, "Choose a valid team or house location.")
    return f"{kind}:{reference.strip()}"


def team_location(name, department=""):
    name = str(name or "").strip()
    department = str(department or "").strip()
    if not name:
        raise ApiError(400, "Team name is required.")
    if department:
        return f"team:{quote(department, safe='')}|{quote(name, safe='')}"
    if ":" in name:
        raise ApiError(400, "Team names cannot contain a colon.")
    if "|" in name:
        return f"team:|{quote(name, safe='')}"
    return f"team:{name}"


def canonical_department(db, name):
    name = str(name or "").strip()
    if not name:
        return ""
    existing = db.execute("SELECT DISTINCT department FROM movements").fetchall()
    for row in existing:
        if row["department"].casefold() == name.casefold():
            return row["department"]
    return name


def canonical_team(db, name, department=""):
    name = str(name or "").strip()
    if not name:
        return ""
    existing = db.execute(
        "SELECT team, department FROM movements WHERE team != ''"
    ).fetchall()
    for row in existing:
        if (
            row["team"].casefold() == name.casefold()
            and row["department"].casefold() == department.casefold()
        ):
            return row["team"]
    return name


def parse_quantity(value, label="Quantity", allow_zero=False):
    try:
        qty = float(value)
    except (TypeError, ValueError):
        raise ApiError(400, f"{label} must be a number.")
    if not math.isfinite(qty) or qty < 0 or (qty == 0 and not allow_zero):
        condition = "zero or greater" if allow_zero else "greater than zero"
        raise ApiError(400, f"{label} must be {condition}.")
    return qty


def parse_legacy_date(value, row_number):
    if isinstance(value, (datetime, date)):
        return value.date().isoformat() if isinstance(value, datetime) else value.isoformat()
    text = str(value or "").strip()
    for pattern in ("%Y-%m-%d", "%d/%m/%Y", "%d/%m/%y", "%d-%m-%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(text, pattern).date().isoformat()
        except ValueError:
            continue
    raise ApiError(400, f"MOVEMENTS row {row_number} has an invalid date.")


def known_locations(db):
    locations = {"warehouse"}
    locations.update(
        f"house:{row['id']}" for row in db.execute("SELECT id FROM houses")
    )
    for row in db.execute(
        "SELECT DISTINCT team, department FROM movements WHERE team != ''"
    ):
        locations.add(team_location(row["team"], row["department"]))
    for row in db.execute("SELECT source, destination FROM movements"):
        locations.add(row["source"])
        locations.add(row["destination"])
    for row in db.execute("SELECT location FROM stocktakes"):
        locations.add(row["location"])
    return locations


def balances_for(db, material_id):
    material = db.execute(
        "SELECT opening_qty FROM materials WHERE id = ?", (material_id,)
    ).fetchone()
    if material is None:
        raise ApiError(404, "Material not found.")

    locations = known_locations(db)
    locations.add("warehouse")
    balances = {}
    movements = db.execute(
        "SELECT id, qty, source, destination FROM movements "
        "WHERE material_id = ? ORDER BY id",
        (material_id,),
    ).fetchall()
    for location in locations:
        snapshot = db.execute(
            "SELECT quantity, movement_id FROM stocktakes "
            "WHERE material_id = ? AND location = ? ORDER BY id DESC LIMIT 1",
            (material_id, location),
        ).fetchone()
        if snapshot:
            balance = snapshot["quantity"]
            after_id = snapshot["movement_id"]
        elif location == "warehouse":
            balance = material["opening_qty"]
            after_id = 0
        else:
            balance = 0.0
            after_id = 0

        if balance is not None:
            for movement in movements:
                if movement["id"] <= after_id:
                    continue
                if movement["source"] == location:
                    balance -= movement["qty"]
                if movement["destination"] == location:
                    balance += movement["qty"]
        balances[location] = balance
    return balances


def _valid_reference(db, location):
    kind, _, reference = location.partition(":")
    if location in {"warehouse", "consumed"}:
        return
    if kind == "house":
        exists = db.execute("SELECT 1 FROM houses WHERE id = ?", (reference,)).fetchone()
    elif kind == "team" and reference.strip():
        exists = True
    else:
        exists = False
    if not exists:
        raise ApiError(400, "A selected house or team location is invalid.")


def _upsert_house(db, house_id, postcode="", owner_name="", employee_name=""):
    if not house_id or not str(house_id).strip():
        raise ApiError(400, "House ID is required for this movement.")
    house_id = str(house_id).strip()
    db.execute(
        """
        INSERT INTO houses(id, postcode, owner_name, employee_name) VALUES (?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            postcode = CASE WHEN excluded.postcode = '' THEN houses.postcode ELSE excluded.postcode END,
            owner_name = CASE WHEN excluded.owner_name = '' THEN houses.owner_name ELSE excluded.owner_name END,
            employee_name = CASE WHEN excluded.employee_name = '' THEN houses.employee_name ELSE excluded.employee_name END
        """,
        (
            house_id,
            str(postcode or "").strip(),
            str(owner_name or "").strip(),
            str(employee_name or "").strip(),
        ),
    )
    return house_id


def _next_delivery_number(db, house_id):
    if not house_id:
        raise ApiError(400, "House ID is required for a delivery.")
    return db.execute(
        """
        SELECT COALESCE(MAX(delivery_number), 0) + 1
        FROM movements
        WHERE house_id = ? AND kind IN ('delivery', 'pickup')
        """,
        (house_id,),
    ).fetchone()[0]


def _record_movement(db, record, check_balance=True, delivery_number=None):
    kind = str(record.get("kind", "")).strip()
    if kind not in MOVEMENT_TYPES:
        raise ApiError(400, "Choose a valid movement type.")
    happened_on = str(record.get("date") or date.today().isoformat()).strip()
    try:
        datetime.strptime(happened_on, "%Y-%m-%d")
    except ValueError:
        raise ApiError(400, "Date must use YYYY-MM-DD.")

    material_id = record.get("material_id")
    material = db.execute(
        "SELECT id FROM materials WHERE id = ?", (material_id,)
    ).fetchone()
    if material is None:
        raise ApiError(400, "Choose a valid material.")
    qty = parse_quantity(record.get("qty"))
    department = canonical_department(db, record.get("department"))
    team = canonical_team(db, record.get("team"), department)
    person = str(record.get("person") or "").strip()
    notes = str(record.get("notes") or "").strip()
    if len(notes) > 2000:
        raise ApiError(400, "Notes must be 2,000 characters or fewer.")
    house_id = str(record.get("house_id") or "").strip()
    postcode = str(record.get("postcode") or "").strip()
    owner_name = str(record.get("owner_name") or "").strip()
    employee_name = str(record.get("employee_name") or "").strip()
    if kind in {"pickup", "delivery", "use", "return"} and not house_id:
        raise ApiError(400, "House ID is required for this movement.")
    if house_id:
        house_id = _upsert_house(db, house_id, postcode, owner_name, employee_name)

    if kind == "collection":
        if not team or not person:
            raise ApiError(400, "Record the responsible person and team for every movement.")
        source, destination = "warehouse", team_location(team, department)
    elif kind == "pickup":
        if not team or not person:
            raise ApiError(400, "Record the responsible person and team for every movement.")
        source, destination = "warehouse", location_key("house", house_id)
    elif kind == "delivery":
        source_kind = record.get("source_kind", "warehouse")
        if source_kind not in {"warehouse", "team"}:
            raise ApiError(400, "A delivery must come from the warehouse or a team.")
        source_department = canonical_department(
            db, record.get("source_department") or department
        )
        source_team = canonical_team(
            db, record.get("source_team") or team, source_department
        )
        source = (
            "warehouse"
            if source_kind == "warehouse"
            else team_location(source_team, source_department)
        )
        destination = location_key("house", house_id)
        team = team or (source_team if source_kind == "team" else "")
        department = department or (source_department if source_kind == "team" else "")
    elif kind in {"use", "return"}:
        source = location_key("house", house_id)
        destination = "consumed" if kind == "use" else "warehouse"
    else:
        source_kind = str(record.get("source_kind") or "")
        destination_kind = str(record.get("destination_kind") or "")
        if source_kind not in {"team", "house"} or destination_kind not in {"team", "house"}:
            raise ApiError(400, "Transfers must be between a team and/or a house.")
        source_reference = str(record.get("source_reference") or "").strip()
        destination_reference = str(record.get("destination_reference") or "").strip()
        if source_kind == "house":
            _upsert_house(
                db,
                source_reference,
                record.get("source_postcode"),
            )
        if destination_kind == "house":
            _upsert_house(
                db,
                destination_reference,
                record.get("destination_postcode"),
            )
        if source_kind == "team":
            source_department = canonical_department(db, record.get("source_department"))
            source_reference = canonical_team(db, source_reference, source_department)
            source = team_location(source_reference, source_department)
        else:
            source = location_key(source_kind, source_reference)
        if destination_kind == "team":
            destination_department = canonical_department(
                db, record.get("destination_department")
            )
            destination_reference = canonical_team(
                db, destination_reference, destination_department
            )
            destination = team_location(destination_reference, destination_department)
        else:
            destination = location_key(destination_kind, destination_reference)
        if source == destination:
            raise ApiError(400, "Choose different source and destination locations.")
        if not house_id:
            if source_kind == "house":
                house_id = source_reference
            elif destination_kind == "house":
                house_id = destination_reference
        team = team or (
            source_reference if source_kind == "team" else (
                destination_reference if destination_kind == "team" else ""
            )
        )
        department = department or (
            source_department if source_kind == "team" else (
                destination_department if destination_kind == "team" else ""
            )
        )

    if not person or not team:
        raise ApiError(400, "Record the responsible person and team for every movement.")
    _valid_reference(db, source)
    _valid_reference(db, destination)
    if check_balance:
        available = balances_for(db, material_id).get(source)
        if available is None:
            raise ApiError(
                409,
                "The source balance is unknown. Record a stocktake before moving material from this location.",
            )
        if available + 1e-9 < qty:
            raise ApiError(
                409,
                f"Insufficient stock at the source location (available: {available:g}).",
            )

    cursor = db.execute(
        """
        INSERT INTO movements
            (happened_on, kind, material_id, qty, source, destination, house_id, delivery_number, person, team, department, notes)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            happened_on, kind, material_id, qty, source, destination, house_id or None,
            (
                delivery_number or _next_delivery_number(db, house_id)
                if kind in {"delivery", "pickup"}
                else None
            ),
            person, team, department, notes,
        ),
    )
    return cursor.lastrowid


def _record_movement_batch(db, payload):
    if not isinstance(payload, dict):
        raise ApiError(400, "Movement data must be a JSON object.")
    items = payload.get("items")
    if not isinstance(items, list) or not items:
        raise ApiError(400, "Add at least one material to this movement.")
    if len(items) > 200:
        raise ApiError(400, "A movement can include at most 200 materials.")

    batch_delivery_number = None
    if payload.get("kind") in {"delivery", "pickup"}:
        batch_delivery_number = _next_delivery_number(db, payload.get("house_id"))
    if payload.get("kind") == "pickup" and "is_extra_delivery" in payload:
        is_extra_delivery = payload["is_extra_delivery"]
        if not isinstance(is_extra_delivery, bool):
            raise ApiError(400, "Extra delivery must be selected or cleared.")
        if is_extra_delivery and batch_delivery_number == 1:
            raise ApiError(400, "Record the first warehouse pickup before marking an extra delivery.")
        if not is_extra_delivery and batch_delivery_number > 1:
            raise ApiError(400, "Mark this pickup as an extra delivery to record it as delivery #2 or later.")

    common = {key: value for key, value in payload.items() if key != "items"}
    movement_ids = []
    for item in items:
        if not isinstance(item, dict):
            raise ApiError(400, "Each material entry must be an object.")
        record = {
            **common,
            "material_id": item.get("material_id"),
            "qty": item.get("qty"),
        }
        movement_ids.append(
            _record_movement(db, record, delivery_number=batch_delivery_number)
        )
    return movement_ids, batch_delivery_number


def _movement_json(row):
    return {
        "id": row["id"],
        "date": row["happened_on"],
        "kind": row["kind"],
        "material_id": row["material_id"],
        "material": row["material"],
        "qty": row["qty"],
        "source": row["source"],
        "destination": row["destination"],
        "house_id": row["house_id"],
        "house_owner": row["house_owner"],
        "house_employee": row["house_employee"],
        "house_postcode": row["house_postcode"],
        "delivery_number": row["delivery_number"],
        "person": row["person"],
        "team": row["team"],
        "department": row["department"],
        "notes": row["notes"],
    }


def _movement_rows(db, limit=100, query=""):
    query = str(query or "").strip()
    sql = """
        SELECT m.*, p.name AS material, h.owner_name AS house_owner,
            h.employee_name AS house_employee, h.postcode AS house_postcode
        FROM movements m
        JOIN materials p ON p.id = m.material_id
        LEFT JOIN houses h ON h.id = m.house_id
    """
    if query:
        searchable = (
            "m.happened_on", "m.kind", "p.name", "m.source", "m.destination",
            "m.house_id", "h.owner_name", "h.employee_name", "h.postcode",
            "m.person", "m.team", "m.department", "m.notes",
        )
        sql += " WHERE " + " OR ".join(
            "instr(lower(COALESCE({}, '')), lower(?)) > 0".format(column)
            for column in searchable
        )
        sql += " ORDER BY m.id DESC"
        return db.execute(sql, (query,) * len(searchable)).fetchall()
    return db.execute(sql + " ORDER BY m.id DESC LIMIT ?", (limit,)).fetchall()


def _dashboard(db):
    materials = []
    for row in db.execute("SELECT * FROM materials ORDER BY name COLLATE NOCASE"):
        balances = balances_for(db, row["id"])
        warehouse = balances.get("warehouse")
        negative_locations = [
            location
            for location, quantity in balances.items()
            if location != "consumed" and quantity is not None and quantity < -1e-9
        ]
        materials.append(
            {
                "id": row["id"],
                "code": row["code"],
                "name": row["name"],
                "unit": row["unit"],
                "opening_qty": row["opening_qty"],
                "warehouse_qty": warehouse,
                "balances": balances,
                "negative_locations": negative_locations,
                "needs_stocktake": warehouse is None or bool(negative_locations),
            }
        )
    house_count = db.execute("SELECT COUNT(*) FROM houses").fetchone()[0]
    active_house_count = db.execute(
        "SELECT COUNT(*) FROM houses WHERE is_finished = 0"
    ).fetchone()[0]
    return {
        "materials": materials,
        "demo_mode": (
            db.execute(
                "SELECT value FROM app_settings WHERE key = 'demo_mode'"
            ).fetchone() or {"value": "0"}
        )["value"] == "1",
        "houses": active_house_count,
        "movements": [_movement_json(row) for row in _movement_rows(db, 12)],
        "teams": sorted(
            {
                " · ".join(filter(None, (row["department"], row["team"])))
                for row in db.execute(
                    "SELECT DISTINCT department, team FROM movements WHERE team != ''"
                )
            },
            key=str.casefold,
        ),
        "house_list": [
            dict(row)
            for row in db.execute(
                """
                SELECT id, postcode, owner_name, employee_name, is_finished
                FROM houses ORDER BY id COLLATE NOCASE
                """
            )
        ],
        "locations": sorted(known_locations(db)),
    }


def _import_workbook(db, path):
    try:
        from openpyxl import load_workbook
        from openpyxl.utils.exceptions import InvalidFileException
    except ImportError:
        raise ApiError(501, "Excel import requires openpyxl. Install dependencies from requirements.txt.")

    try:
        workbook = load_workbook(path, data_only=True, read_only=True)
    except (OSError, ValueError, zipfile.BadZipFile, ParseError, InvalidFileException) as exc:
        raise ApiError(400, f"Could not read this Excel workbook: {exc}")
    if "STOCK" not in workbook.sheetnames or "MOVEMENTS" not in workbook.sheetnames:
        raise ApiError(400, "Workbook must contain STOCK and MOVEMENTS sheets.")
    if db.execute("SELECT 1 FROM materials LIMIT 1").fetchone():
        raise ApiError(409, "Import is only allowed into an empty tracker to prevent duplicate records.")

    stock = workbook["STOCK"]
    movement_sheet = workbook["MOVEMENTS"]
    stock_headers = {
        str(stock.cell(1, column).value or "").strip().casefold(): column
        for column in range(1, stock.max_column + 1)
    }
    movement_headers = {
        str(movement_sheet.cell(1, column).value or "").strip().casefold(): column
        for column in range(1, movement_sheet.max_column + 1)
    }
    required_stock = {"material", "starting stock"}
    required_movements = {"date", "house id", "material", "qty", "movement"}
    if not required_stock.issubset(stock_headers) or not required_movements.issubset(
        movement_headers
    ):
        raise ApiError(400, "Workbook columns do not match the expected tracker layout.")

    warnings = []
    material_ids = {}
    used_names = set()
    for row_number in range(2, stock.max_row + 1):
        name = stock.cell(row_number, stock_headers["material"]).value
        if name is None or not str(name).strip():
            continue
        name = str(name).strip()
        normalized = name.casefold()
        if normalized in used_names:
            raise ApiError(400, f"Duplicate material name in STOCK row {row_number}.")
        used_names.add(normalized)
        raw_qty = stock.cell(row_number, stock_headers["starting stock"]).value
        opening_qty = None
        if raw_qty is not None and str(raw_qty).strip() not in {"", "?"}:
            opening_qty = parse_quantity(raw_qty, f"STOCK row {row_number} starting stock", True)
        else:
            warnings.append(f"{name}: starting stock is unknown; record a stocktake before collection.")
        code_col = stock_headers.get("material id")
        code = stock.cell(row_number, code_col).value if code_col else None
        cursor = db.execute(
            "INSERT INTO materials(code, name, opening_qty) VALUES (?, ?, ?)",
            (str(code).strip() if code is not None else "", name, opening_qty),
        )
        material_ids[normalized] = cursor.lastrowid

    def value(row_number, column):
        index = movement_headers.get(column)
        return movement_sheet.cell(row_number, index).value if index else None

    missing_accountability = 0
    skipped_zero_movements = 0
    for row_number in range(2, movement_sheet.max_row + 1):
        name = value(row_number, "material")
        if name is None or not str(name).strip():
            continue
        name = str(name).strip()
        raw_qty = value(row_number, "qty")
        try:
            qty = parse_quantity(
                raw_qty, f"MOVEMENTS row {row_number} quantity", allow_zero=True
            )
        except ApiError as exc:
            raise ApiError(400, str(exc))
        if qty == 0:
            skipped_zero_movements += 1
            continue

        happened_on = parse_legacy_date(value(row_number, "date"), row_number)

        original_kind = str(value(row_number, "movement") or "").strip().casefold()
        kind_map = {
            "delivery to house": "delivery",
            "return to warehouse": "return",
            "used on house": "use",
        }
        kind = kind_map.get(original_kind)
        if kind is None:
            raise ApiError(400, f"MOVEMENTS row {row_number} has an unsupported movement type.")
        house_id = value(row_number, "house id")
        if house_id is None or not str(house_id).strip():
            raise ApiError(400, f"MOVEMENTS row {row_number} is missing its House ID.")
        house_id = str(house_id).strip()
        normalized = name.casefold()
        material_id = material_ids.get(normalized)
        if material_id is None:
            cursor = db.execute(
                "INSERT INTO materials(code, name, opening_qty) VALUES ('', ?, 0)",
                (name,),
            )
            material_id = cursor.lastrowid
            material_ids[normalized] = material_id
            warnings.append(f"{name}: no STOCK row found; imported with zero opening stock.")
        _upsert_house(
            db,
            house_id,
            value(row_number, "postcode / eircode"),
            value(row_number, "house owner name"),
        )
        source = location_key("house", house_id) if kind in {"return", "use"} else "warehouse"
        destination = "warehouse" if kind == "return" else (
            "consumed" if kind == "use" else location_key("house", house_id)
        )
        person = str(value(row_number, "delivered by") or "").strip()
        team = str(value(row_number, "worker team") or "").strip()
        if not person or not team:
            missing_accountability += 1
        notes = str(value(row_number, "notes") or "").strip()
        db.execute(
            """
            INSERT INTO movements
                (happened_on, kind, material_id, qty, source, destination, house_id, delivery_number, person, team, department, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                happened_on, kind, material_id, qty, source, destination, house_id,
                _next_delivery_number(db, house_id) if kind == "delivery" else None,
                person, team, "", notes,
            ),
        )

    if missing_accountability:
        warnings.append(
            f"{missing_accountability} historical movement(s) have no person or team recorded."
        )
    if skipped_zero_movements:
        warnings.append(
            f"{skipped_zero_movements} zero-quantity movement row(s) were skipped."
        )
    negative_balances = 0
    for row in db.execute("SELECT id FROM materials"):
        for location, quantity in balances_for(db, row["id"]).items():
            if location != "consumed" and quantity is not None and quantity < -1e-9:
                negative_balances += 1
    if negative_balances:
        warnings.append(
            f"{negative_balances} warehouse, team or house balance(s) are negative; reconcile them with physical stocktakes."
        )
    return {
        "material_count": len(material_ids),
        "movement_count": db.execute("SELECT COUNT(*) FROM movements").fetchone()[0],
        "house_count": db.execute("SELECT COUNT(*) FROM houses").fetchone()[0],
        "warnings": warnings,
    }


class RequestHandler(BaseHTTPRequestHandler):
    server_version = "EWITracker/1.0"

    def log_message(self, format_string, *args):
        print("%s - %s" % (self.address_string(), format_string % args), file=sys.stderr)

    def _send_json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path, content_type):
        try:
            body = path.read_bytes()
        except OSError:
            self._send_json(404, {"error": "Page not found."})
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise ApiError(400, "Invalid request body.")
        if length <= 0 or length > 10_000_000:
            raise ApiError(400, "Request body is empty or too large.")
        try:
            return self.rfile.read(length)
        except OSError:
            raise ApiError(400, "Could not read request body.")

    def _json_body(self):
        try:
            return json.loads(self._body())
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise ApiError(400, "Request body must be valid JSON.")

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/":
            self._send_file(STATIC_DIR / "index.html", "text/html; charset=utf-8")
        elif path == "/app.css":
            self._send_file(STATIC_DIR / "app.css", "text/css; charset=utf-8")
        elif path == "/app.js":
            self._send_file(STATIC_DIR / "app.js", "text/javascript; charset=utf-8")
        elif path == "/brusna-logo.jpg":
            self._send_file(STATIC_DIR / "brusna-logo.jpg", "image/jpeg")
        elif path == "/api/dashboard":
            with connect_db() as db:
                self._send_json(200, _dashboard(db))
        elif path == "/api/movements":
            query = parse_qs(urlparse(self.path).query).get("q", [""])[0]
            with connect_db() as db:
                self._send_json(
                    200,
                    {"movements": [_movement_json(row) for row in _movement_rows(db, 500, query)]},
                )
        else:
            self._send_json(404, {"error": "Page not found."})

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            if path in {"/api/movements", "/api/movements/batch"}:
                payload = self._json_body()
                if path.endswith("/batch"):
                    with connect_db() as db:
                        with db:
                            movement_ids, delivery_number = _record_movement_batch(db, payload)
                else:
                    if not isinstance(payload, dict):
                        raise ApiError(400, "Movement data must be a JSON object.")
                    with connect_db() as db:
                        with db:
                            movement_ids = [_record_movement(db, payload)]
                            delivery_number = None
                            if payload.get("kind") == "delivery":
                                delivery_number = db.execute(
                                    "SELECT delivery_number FROM movements WHERE id = ?",
                                    (movement_ids[0],),
                                ).fetchone()[0]
                if path.endswith("/batch"):
                    self._send_json(
                        201,
                        {"ids": movement_ids, "delivery_number": delivery_number},
                    )
                else:
                    self._send_json(
                        201,
                        {"id": movement_ids[0], "delivery_number": delivery_number},
                    )
            elif path == "/api/stocktakes":
                payload = self._json_body()
                material_id = payload.get("material_id")
                location = str(payload.get("location") or "").strip()
                quantity = parse_quantity(payload.get("quantity"), "Counted quantity", True)
                counted_by = str(payload.get("counted_by") or "").strip()
                notes = str(payload.get("notes") or "").strip()
                if len(notes) > 2000:
                    raise ApiError(400, "Notes must be 2,000 characters or fewer.")
                with connect_db() as db:
                    with db:
                        if not db.execute(
                            "SELECT 1 FROM materials WHERE id = ?", (material_id,)
                        ).fetchone():
                            raise ApiError(400, "Choose a valid material.")
                        if location == "consumed" or location not in known_locations(db):
                            raise ApiError(400, "Choose a warehouse, team, or house with a recorded balance.")
                        _valid_reference(db, location)
                        if not counted_by:
                            raise ApiError(400, "Record who completed the stocktake.")
                        movement_id = db.execute(
                            "SELECT COALESCE(MAX(id), 0) FROM movements"
                        ).fetchone()[0]
                        db.execute(
                            """
                            INSERT INTO stocktakes
                                (material_id, location, quantity, movement_id, counted_on, counted_by, notes)
                            VALUES (?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                material_id,
                                location,
                                quantity,
                                movement_id,
                                date.today().isoformat(),
                                counted_by,
                                notes,
                            ),
                        )
                self._send_json(201, {"ok": True})
            elif path == "/api/materials":
                payload = self._json_body()
                name = str(payload.get("name") or "").strip()
                code = str(payload.get("code") or "").strip()
                unit = str(payload.get("unit") or "units").strip() or "units"
                opening_qty = parse_quantity(
                    payload.get("opening_qty"), "Opening stock", allow_zero=True
                )
                if not name or len(name) > 200:
                    raise ApiError(400, "Material name is required (maximum 200 characters).")
                if len(code) > 100 or len(unit) > 40:
                    raise ApiError(400, "Material code or unit is too long.")
                with connect_db() as db:
                    with db:
                        cursor = db.execute(
                            "INSERT INTO materials(code, name, unit, opening_qty) VALUES (?, ?, ?, ?)",
                            (code, name, unit, opening_qty),
                        )
                self._send_json(201, {"id": cursor.lastrowid})
            elif path == "/api/houses":
                payload = self._json_body()
                if not isinstance(payload, dict):
                    raise ApiError(400, "House data must be a JSON object.")
                house_id = str(payload.get("id") or "").strip()
                if len(house_id) > 120:
                    raise ApiError(400, "House ID must be 120 characters or fewer.")
                postcode = str(payload.get("postcode") or "").strip()
                owner_name = str(payload.get("owner_name") or "").strip()
                employee_name = str(payload.get("employee_name") or "").strip()
                if len(postcode) > 80 or len(owner_name) > 160 or len(employee_name) > 160:
                    raise ApiError(400, "House details exceed the allowed length.")
                with connect_db() as db:
                    with db:
                        _upsert_house(
                            db,
                            house_id,
                            postcode,
                            owner_name,
                            employee_name,
                        )
                self._send_json(201, {"id": house_id})
            elif path == "/api/houses/status":
                payload = self._json_body()
                if not isinstance(payload, dict) or not isinstance(payload.get("finished"), bool):
                    raise ApiError(400, "Choose whether the house is finished.")
                house_id = str(payload.get("id") or "").strip()
                if not house_id:
                    raise ApiError(400, "House ID is required.")
                with connect_db() as db:
                    with db:
                        cursor = db.execute(
                            "UPDATE houses SET is_finished = ? WHERE id = ?",
                            (int(payload["finished"]), house_id),
                        )
                        if not cursor.rowcount:
                            raise ApiError(404, "House was not found.")
                self._send_json(200, {"id": house_id, "finished": payload["finished"]})
            elif path == "/api/import":
                content_type = self.headers.get("Content-Type", "")
                if not content_type.startswith("multipart/form-data;"):
                    raise ApiError(400, "Upload an Excel workbook using the file picker.")
                body = self._body()
                match = re.search(r'boundary="?([^";]+)"?', content_type)
                if not match:
                    raise ApiError(400, "Invalid file upload.")
                boundary = match.group(1).encode("ascii")
                parts = body.split(b"--" + boundary)
                file_data = None
                for part in parts:
                    if b"filename=" in part and b"\r\n\r\n" in part:
                        file_data = part.split(b"\r\n\r\n", 1)[1].rsplit(b"\r\n", 1)[0]
                        break
                if not file_data:
                    raise ApiError(400, "Choose an Excel workbook to import.")
                temporary_file = ROOT / "instance" / "workbook-upload.xlsx"
                temporary_file.parent.mkdir(parents=True, exist_ok=True)
                try:
                    temporary_file.write_bytes(file_data)
                    with connect_db() as db:
                        with db:
                            result = _import_workbook(db, temporary_file)
                    self._send_json(201, result)
                finally:
                    temporary_file.unlink(missing_ok=True)
            else:
                self._send_json(404, {"error": "Page not found."})
        except ApiError as exc:
            self._send_json(exc.status, {"error": str(exc)})
        except sqlite3.IntegrityError as exc:
            message = (
                "A material with that name already exists."
                if "materials.name" in str(exc)
                else "This record conflicts with existing tracker data."
            )
            self._send_json(409, {"error": message})
        except (OSError, sqlite3.Error) as exc:
            self._send_json(500, {"error": f"Could not save the record: {exc}"})


def run():
    initialize_db()
    server = ThreadingHTTPServer(("127.0.0.1", PORT), RequestHandler)
    print(f"EWI Tracker demo running at http://127.0.0.1:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping EWI Tracker.")
    finally:
        server.server_close()


if __name__ == "__main__":
    run()
