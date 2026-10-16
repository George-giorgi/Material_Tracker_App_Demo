import json
from datetime import date
from pathlib import Path

import server


ROOT = Path(__file__).resolve().parent
CATALOG_PATH = ROOT / "demo_catalog.json"


def seed_demo():
    materials = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    server.initialize_db()
    with server.connect_db() as db:
        if db.execute("SELECT 1 FROM materials LIMIT 1").fetchone():
            raise RuntimeError(
                "The demo database already has materials; refusing to overwrite it."
            )
        with db:
            material_ids = []
            for index, material in enumerate(materials):
                opening_qty = 40 + (index * 17 % 121)
                cursor = db.execute(
                    """
                    INSERT INTO materials(code, name, unit, opening_qty)
                    VALUES (?, ?, 'units', ?)
                    """,
                    (material["code"], material["name"], opening_qty),
                )
                material_ids.append(cursor.lastrowid)

            db.execute(
                "INSERT OR REPLACE INTO app_settings(key, value) VALUES ('demo_mode', '1')"
            )
            server._upsert_house(
                db, "DEMO-101", "D01 DEMO", "Casey Example", "Jamie Demo"
            )
            server._upsert_house(
                db, "DEMO-202", "D02 DEMO", "Morgan Example", "Taylor Demo"
            )

            common = {
                "date": date.today().isoformat(),
                "kind": "pickup",
                "house_id": "DEMO-101",
                "owner_name": "Casey Example",
                "employee_name": "Jamie Demo",
                "person": "Jamie Demo",
                "team": "Demo Crew",
                "items": [
                    {"material_id": material_ids[0], "qty": 8},
                    {"material_id": material_ids[1], "qty": 6},
                    {"material_id": material_ids[2], "qty": 10},
                ],
            }
            server._record_movement_batch(
                db, {**common, "is_extra_delivery": False}
            )
            server._record_movement_batch(
                db,
                {
                    **common,
                    "items": [{"material_id": material_ids[0], "qty": 3}],
                    "is_extra_delivery": True,
                    "notes": "Fictional sample extra delivery.",
                },
            )
            server._record_movement_batch(
                db,
                {
                    **common,
                    "house_id": "DEMO-202",
                    "owner_name": "Morgan Example",
                    "employee_name": "Taylor Demo",
                    "items": [{"material_id": material_ids[3], "qty": 7}],
                },
            )
            server._record_movement(
                db,
                {
                    "kind": "return",
                    "date": date.today().isoformat(),
                    "house_id": "DEMO-101",
                    "person": "Jamie Demo",
                    "team": "Demo Crew",
                    "material_id": material_ids[1],
                    "qty": 2,
                    "notes": "Fictional sample return.",
                },
            )
            server._record_movement(
                db,
                {
                    "kind": "transfer",
                    "date": date.today().isoformat(),
                    "source_kind": "house",
                    "source_reference": "DEMO-101",
                    "destination_kind": "house",
                    "destination_reference": "DEMO-202",
                    "person": "Jamie Demo",
                    "team": "Demo Crew",
                    "material_id": material_ids[0],
                    "qty": 2,
                    "notes": "Fictional sample site transfer.",
                },
            )
    return len(materials)


if __name__ == "__main__":
    count = seed_demo()
    print(
        f"Seeded {count} materials and fictional demo activity in "
        f"{server.DB_PATH}. No live tracker data was copied."
    )
