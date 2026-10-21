# BRUSNA Material Tracker — Demo

This is an isolated demo copy of the tracker. It has its own SQLite database
at `instance/ewi.sqlite3`, uses port `8001`, and does not read or modify the
live project database.

The demo contains the material names and codes from the Excel stock list,
fictional warehouse quantities, and fictional houses, employees, and movements.
It contains no imported house or movement records from the live tracker. The
app displays a **DEMO DATA — NOT REAL STOCK** warning.

## Run the demo

Use Python 3.10 or newer. On Windows PowerShell:

```powershell
py -m pip install -r requirements.txt
py server.py
```

Open [http://127.0.0.1:8001](http://127.0.0.1:8001). On the first launch,
create the administrator account with a username and unique 6-digit PIN. The
administrator can create employee accounts from **Account → Manage employees**.
Give each person their own username and PIN; do not share PINs. Users can
change their PIN from **Account**; sessions expire after 12 hours. Five
incorrect PIN attempts lock that username for 15 minutes. A short PIN is less
secure than a password, so only use this sign-in over HTTPS on a trusted
network. If a tracker database already contains an older password-based
account, its existing password still works in the PIN field; change it to a
6-digit PIN after signing in.

Employees can view tracker records and record material movements against
existing houses. They cannot add materials, count or adjust stock, import
workbooks, create houses, or finish/reopen houses. These restrictions are
enforced by the server, not only by hiding buttons in the interface.
Admins and employees can open a house's checklist and save its scaffolding,
ESB cable, and plumber-work statuses. House detail and movement history remain
available to administrators only.

## Share with employees

The default server listens only on this computer. To let other devices connect,
the tracker must run on a host computer that stays on, and that computer's
database is the shared database. First create the administrator account by
running `py server.py` with the default local settings and opening the local
URL above. Stop it with **Ctrl+C**.

For device access, configure a TLS certificate whose names match the host name
employees will use. Then start the tracker from PowerShell, replacing the
certificate paths with the paths on the host:

```powershell
$env:EWI_HOST = '0.0.0.0'
$env:EWI_TLS_CERT = 'C:\path\to\tracker-certificate.pem'
$env:EWI_TLS_KEY = 'C:\path\to\tracker-private-key.pem'
py server.py
```

Employees then open `https://<host-name>:8001` (replace `<host-name>` with the
host computer's DNS name). Install a certificate trusted by employee devices;
do not ask employees to ignore certificate warnings. Allow TCP port 8001 only
on the intended private network in the host firewall. The server refuses to
listen on a network interface without HTTPS, and the initial administrator
must be created locally before network access is enabled. Do not expose this
development server directly to the public internet.

Back up the host's `instance\ewi.sqlite3` regularly. Do not run separate
tracker copies on employee devices; they would have separate databases.

The demo database is already seeded. If you create a fresh demo database,
run `py seed_demo.py` once before starting the server. The seed script
refuses to overwrite a database that already has materials. To keep the demo
independent, do not set `EWI_DB_PATH` to the live tracker's database path.
The demo also includes enough clearly labeled fictional house records to bring
the provided-house list to 50 total entries, preserving any existing records.
Each sample has a postcode, wall area, and perimeter
for testing the automatic calculator. Fictional warehouse opening stock is
increased by 2,000 units for each material. These example houses and quantities
are not real SEAI or stock data and must not be used for real jobs.

## Navigate and test the demo

After signing in as an administrator or employee, click **Navigation test
flow** to step through Overview, Inventory, Houses, Teams, the house map, and
Movements. Use Back, Next, or Close at any time. The tour only changes views;
it does not save changes to stock or house records. Surveyor accounts do not
see the tour because their navigation is limited to surveys.

Run the automated navigation-flow tests with:

```powershell
py -m unittest tests.test_navigation_flow
```

These exercise the real navigation routing logic and check that menu and
shortcut destinations match the app's pages. Run the full application tests
with `py -m unittest discover -s tests`.

For the end-to-end browser test, install the test dependencies and Chromium
once, then run:

```powershell
py -m pip install -r requirements-test.txt
py -m playwright install chromium
py -m unittest tests.test_navigation_browser
```

The browser test creates an administrator and runs the full tour against a
temporary database; it does not use or change the demo database.

## Present the demo

Open the app and click **Presentation guide**. It walks through adding a
fictional house from Ready for start through survey, activation, stage
progress, warehouse pickup, extra delivery, return, crew transfer, completion,
and retained house history. The guide supplies English narration and opens
the relevant views and forms; review and save each form during the presentation.

The demo starts with 50 provided houses marked Not started. House details keep
notes, survey notes, snags, and material movement history together for review.

See [DEMO-PRESENTATION.md](./DEMO-PRESENTATION.md) for the matching spoken
video script and scene order.

## SEAI house map

Click **From SEAI** on Overview to open the house map. The current demo map
and Ready for start list combine the original 50 houses with nine saved
Eircode records. The nine Eircode records are Ready for start; the original
50 houses are Not started. The map opens to an Ireland-wide OpenStreetMap view.
Eligible houses appear in the map's house list even when they have no approved
coordinates. The saved map-test houses appear as approximate demo pins, clearly
distinguished from verified locations; they do not show the exact Eircode
locations. Map markers identify Ready houses and active Stage 1 or 2 houses.
Selecting a marker or list entry opens that house's available workflow actions.
Activating a Ready house copies its saved Eircode and owner from the provided
house record; the house remains on the map with its Active status.

Administrators can import an approved UTF-8 CSV from the map. Required headers
are `house_id`, `eircode`, `latitude`, and `longitude`; optional headers are
`address`, `owner_name`, `area_m2`, and `perimeter_m`. Existing house workflow,
notes, surveys, and history are retained when an ID is updated; rows absent
from the file are not deleted. Use verified SEAI/Eircode data and coordinates:
the demo-generated locations are fictional and are not suitable for live jobs.

House coordinates stay in the tracker and are not submitted to a geocoder.
Map imagery is loaded from the public OpenStreetMap tile service, which can
observe tile requests and the approximate map area being viewed. OpenStreetMap
attribution is displayed on the map.

Map-test records use the `map_test` source and have synthetic coordinates for
demonstration only. Their pins are explicitly labeled approximate and do not
represent the real locations of their Eircodes. Import approved
latitude/longitude coordinates to place these houses accurately on the map.

## House surveys and calculator measurements

Administrators can add employee or surveyor accounts in **Account ? Manage employees**. A surveyor sees only houses marked **Ready for start** and can add a survey note, window and door sizes with quantities, and house-corner lengths. Window corner lengths are calculated automatically from each window's width and two sides. Survey entries are saved per provided house. Employees cannot edit surveys.

When the house is selected in the automatic calculator, saved dimensions and corner lengths are loaded and shown read-only instead of requesting the same measurements again. The calculator continues to accept manual measurements when no surveyed house is selected. Existing user accounts and active sessions are preserved when the database is upgraded for the surveyor role.

Mortar / glue is estimated as the existing one bag per four insulation boards (100 mm and 60 mm combined) **plus** an area allowance of 18 bags per 100 m² of net wall area. Net wall area subtracts measured window and door areas from the wall area, and both parts are rounded up to whole bags before being added. With no openings or plinth deduction, the current examples are: 100 m² = 18 area-based + 35 existing board-based = **53 bags**; 200 m² = 36 + 70 = **106 bags**; 80 m² = 15 + 28 = **43 bags**. The calculator shows both parts and the combined total.

## Tests

```sh
py -m unittest discover -s tests
```
