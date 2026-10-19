"""Fill the provided-houses list with fictional data.

This stands in for the future import of the government-supplied Excel/CSV file.
The real import should write to the same `provided_houses` table
(id, postcode, address, owner_name, area_m2, perimeter_m).

Safe to re-run. Houses are only *provided* here; they become active in the
tracker when materials are first recorded against them.
"""
import random

import server

OWNERS = [
    "Aoife Murphy", "Seán Kelly", "Niamh Walsh", "Conor Byrne", "Orla Ryan",
    "Cian O'Brien", "Saoirse Doyle", "Eoin McCarthy", "Ciara Brennan", "Darragh Quinn",
    "Róisín Lynch", "Tadhg Nolan", "Méabh Fitzgerald", "Fionn Daly", "Aisling Burke",
    "Padraig Gallagher", "Sinéad Kavanagh", "Oisín Healy", "Clodagh Moran", "Rory Flynn",
]
STREETS = [
    "Oak Drive", "Willow Park", "Elm Grove", "Church Road", "Meadow View",
    "Riverside Avenue", "Hillcrest", "Station Road", "Beechwood Lawn", "Maple Close",
]
TOWNS = ["Cork", "Limerick", "Galway", "Waterford", "Kilkenny", "Dundalk"]
LETTERS = "ACDEFHKNPRTVWXY"


def eircode(rng):
    return (
        f"{rng.choice(LETTERS)}{rng.randint(10, 99)} "
        f"{rng.choice(LETTERS)}{rng.randint(0, 9)}{rng.choice(LETTERS)}{rng.randint(0, 9)}"
    )


def seed_provided_houses(count=40):
    rng = random.Random(2131)
    server.initialize_db()
    with server.connect_db() as db:
        # An earlier version put these straight into the active tracker table.
        # Remove them if nothing was ever recorded on them.
        db.execute(
            """
            DELETE FROM houses
            WHERE id GLOB 'WH[0-9][0-9][0-9][0-9]'
              AND CAST(substr(id, 3) AS INTEGER) BETWEEN ? AND ?
              AND NOT EXISTS (SELECT 1 FROM movements WHERE house_id = houses.id)
              AND NOT EXISTS (SELECT 1 FROM material_requests WHERE house_id = houses.id)
            """,
            (2131, 2131 + count - 1),
        )
        for index in range(count):
            house_id = f"WH{2131 + index}"
            perimeter = round(rng.uniform(28, 58), 1)
            area = round(perimeter * rng.uniform(5.2, 6.8), 1)
            address = f"{rng.randint(1, 60)} {rng.choice(STREETS)}, {rng.choice(TOWNS)}"
            db.execute(
                """
                INSERT INTO provided_houses
                    (id, postcode, address, owner_name, area_m2, perimeter_m, source)
                VALUES (?, ?, ?, ?, ?, ?, 'government')
                ON CONFLICT(id) DO UPDATE SET
                    address = CASE WHEN provided_houses.address = ''
                                   THEN excluded.address ELSE provided_houses.address END,
                    area_m2 = COALESCE(provided_houses.area_m2, excluded.area_m2),
                    perimeter_m = COALESCE(provided_houses.perimeter_m, excluded.perimeter_m)
                """,
                (house_id, eircode(rng), address, OWNERS[index % len(OWNERS)], area, perimeter),
            )
    return count


if __name__ == "__main__":
    n = seed_provided_houses()
    print(f"Provided list now includes {n} fictional houses (WH2131 onwards).")
