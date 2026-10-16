# BRUSNA Material Tracker — Demo

This is an isolated demo copy of the tracker. It has its own SQLite database
at `instance/ewi.sqlite3`, uses port `8001`, and does not read or modify the
live project database.

The demo contains the material names and codes from the Excel stock list,
fictional warehouse quantities, and fictional houses, employees, and movements.
It contains no imported house or movement records from the live tracker. The
app displays a **DEMO DATA — NOT REAL STOCK** warning.

## Run the demo

Use Python 3.10 or newer:

```sh
python3 -m pip install -r requirements.txt
python3 server.py
```

Open [http://127.0.0.1:8001](http://127.0.0.1:8001). The current tracker
continues to use its own database and port `8000`.

The demo database is already seeded. If you create a fresh demo database,
run `python3 seed_demo.py` once before starting the server. The seed script
refuses to overwrite a database that already has materials. To keep the demo
independent, do not set `EWI_DB_PATH` to the live tracker's database path.

## Present the demo

Open the app and click **Presentation guide**. It walks through adding a
fictional house, recording a warehouse pickup and extra delivery, returning
unused material, transferring stock to another site, reviewing balances,
finishing the house, and searching its movement history. The guide supplies
English narration and opens the relevant forms prefilled; review and save each
form during the presentation.

See [DEMO-PRESENTATION.md](./DEMO-PRESENTATION.md) for the matching spoken
video script and scene order.

## Tests

```sh
python3 -m unittest discover -s tests
```
