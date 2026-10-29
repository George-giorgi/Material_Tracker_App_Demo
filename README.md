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
administrator can create Admin, Assign, Boss, Surveyor, Warehouse, Delivery,
Supervisor, and Teamlead accounts from **Account → Manage accounts**. Employee
accounts remain available for compatibility.
Give each person their own username and PIN; do not share PINs. Users can
change their PIN from **Account**; sessions expire after 12 hours. Five
incorrect PIN attempts lock that username for 15 minutes. A short PIN is less
secure than a password, so only use this sign-in over HTTPS on a trusted
network. If a tracker database already contains an older password-based
account, its existing password still works in the PIN field; change it to a
6-digit PIN after signing in.

The database maintains a role catalog for Admin, Assign, Boss, Surveyor,
Warehouse, Delivery, Supervisor, and Teamlead, plus the existing Employee role
for compatibility. Admin is assigned the `*` permission, meaning all
application features and operations. Other roles have no permissions assigned
until their access rules are defined. Creating those accounts does not yet
change the app's existing authorization behavior.

## Run the separate SEAI Red dataset

The separate `instance\ewi.seai-red.sqlite3` database is a two-house template
containing the oldest and newest of the 59 Red SEAI applications captured from
the portal: WH570150 and WH618775. Other houses and their linked activity were
removed from this copy; the original `instance\ewi.sqlite3` is unchanged. The
application shows these two records in the normal house list. Their full
captured application sections are stored in `seai_applications`, with measures
in `seai_measures` and attachment filenames/categories in `seai_documents`.
The nine PDF attachments listed in the Application, Survey, and Variation
categories are stored in the separate database and can be opened and annotated
from the Surveyor room. Saving annotations creates a separate PDF copy and
preserves each original SEAI attachment unchanged; non-PDF attachments were not
downloaded. Annotated copies can be shared as complete PDFs using **Share
annotated PDF**, which appends the saved Survey note. In the annotator, select
individual pages across multiple SEAI PDFs to share or download one combined PDF
with the saved Survey note appended as its final page. Moving a house from Ready
for start back to Not started clears its survey records, work plan, notes, snags,
and saved annotations, but preserves SEAI variation quantities. Original PDFs
and status history remain; PDFs are view-only while the house is Not started.
The database retains the demo material catalog and stock data, which are not
real inventory. A pre-change backup is kept beside the database in `instance\`.

The SEAI property GPS fields are blank. Import approved coordinates using the
House map CSV control to add exact pins. Coordinate-mapped houses can appear on
the map while still Not started; the pin does not change their workflow status.
For houses without saved coordinates, select a house and use **Open Eircode pin
in Google Maps** to search its Eircode. Google receives only the Eircode when
someone clicks the link.

### Quick start (PowerShell)

From the project root, copy and run this block to start the SEAI Red database
on port 8011:

```powershell
$env:EWI_DB_PATH = (Join-Path $PWD 'instance\ewi.seai-red.sqlite3')
$env:EWI_PORT = '8011'
& .\.venv\Scripts\python.exe server.py
```

Open [http://127.0.0.1:8011](http://127.0.0.1:8011). It uses the copied
database's existing user accounts. The SEAI records contain sensitive
applicant, property, contact, and notes data; keep this database on a trusted
machine and share it only with authorized people.

Employees can view tracker records and record material movements against
existing houses. They cannot add materials, count or adjust stock, import
workbooks, create houses, or finish/reopen houses. These restrictions are
enforced by the server, not only by hiding buttons in the interface.
Admins and employees can update scaffolding, ESB-work, and plumber-work
completion directly on Active house cards: tick an item to mark it done (green),
or untick it to return it to not done. Work marked not required by the surveyor
is shown as not necessary. Ready-for-start cards keep notes and checklist
editing out of the card; use the Surveyor room for the pre-start work plan.
House detail and movement history remain available to administrators only.
The Teams tab also lists employees assigned to houses at activation and shows
their open and closed house counts; selecting a count opens those houses.

## Oldest-house photo preview

After signing in as an administrator or surveyor, open
`http://127.0.0.1:8001/oldest-house-test` to view a separate, read-only sample
of the earliest application in the exported SEAI list. “Oldest” is based on
application date, not the building's construction year. The sample photos are
served locally, along with that application's six attached PDFs. The preview
includes the captured application, property, contractor project manager, QA,
measures, test result, variance, and notes sections. It does not import or
change tracker house data. It contains sensitive applicant, property, contact,
and notes information. Employee accounts cannot access this page, its photos,
or its documents.

## County weather

The Overview shows the latest published county-level daily forecast for Mayo
and Sligo, including expected conditions, high/low temperatures, rainfall, and
wind. It uses Met Éireann's open county forecast feed; the number of available
days follows the feed and is not extended into a made-up monthly forecast.
Forecasts are county-wide, not precise house or town forecasts. The server
needs outbound internet access to `www.met.ie`; no house coordinates are sent.
Weather is cached in memory for 15 minutes, and the panel has a manual refresh
control. If the service is unavailable, the app reports that instead of
showing stale data. Met Éireann is credited in the panel.

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
Each sample includes fictional occupant and address details, a deliberately
invalid placeholder postcode, wall area, and perimeter for testing the automatic
calculator. Fictional warehouse opening stock is increased by 2,000 units for
each material. These example houses and quantities are not real SEAI or stock
data and must not be used for real jobs.

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
Snag lists and completion toggles are shown once a house is Active; Ready for
start cards and their detail view omit snag controls.

See [DEMO-PRESENTATION.md](./DEMO-PRESENTATION.md) for the matching spoken
video script and scene order.

## SEAI house map

Click **From SEAI** on Overview to open the house map. The current demo map
and Ready for start list combine the original 50 houses with nine saved
Eircode records. The nine Eircode records are Ready for start; the original
50 houses are Not started. The map opens to an Ireland-wide OpenStreetMap view.
Ready and active houses, plus coordinate-mapped Not started houses, appear in
the map's house list. The saved map-test houses appear as approximate demo pins, clearly
distinguished from verified locations; they do not show the exact Eircode
locations. Map markers identify mapped Not started houses, Ready houses, and
active Stage 1 or 2 houses.
Selecting a marker or list entry shows house details and a separate activity
card with status changes, survey activity, movements, and work-stage changes,
including who made each change and when. The sidebar scrolls independently so
the complete eligible-house list remains accessible.
Activating a Ready house copies its saved Eircode and owner from the provided
house record; the house remains on the map with its Active status.

Administrators can import an approved UTF-8 CSV from the map. Required headers
are `house_id`, `eircode`, `latitude`, and `longitude`; optional headers are
`address`, `owner_name`, `area_m2`, and `perimeter_m`. Existing house workflow,
notes, surveys, and history are retained when an ID is updated; rows absent
from the file are not deleted. Use verified SEAI/Eircode data and coordinates:
the demo-generated locations are fictional and are not suitable for live jobs.

For application records captured from SEAI, not-started cards show the four
insulation measure groups with both reported survey quantities. Administrators
can open **View Surveyor room** to review the captured SEAI application sections
and attachment metadata. Administrators can edit **Survey Qty including
Variation** there for not-started houses; surveyors can edit it for ready houses.
Saved quantities are reflected in the house cards. Detail data is returned only
to administrators and surveyors.
House coordinates stay in the tracker and are not submitted to a geocoder.
Map imagery is loaded from the public OpenStreetMap tile service, which can
observe tile requests and the approximate map area being viewed. OpenStreetMap
attribution is displayed on the map.

Map-test records use the `map_test` source and have synthetic coordinates for
demonstration only. Their pins are explicitly labeled approximate and do not
represent the real locations of their Eircodes. Import approved
latitude/longitude coordinates to place these houses accurately on the map.

## House surveys and calculator measurements

Administrators can add employee or surveyor accounts in **Account ? Manage employees**. A surveyor sees only houses marked **Ready for start** and can add survey notes, window and door sizes with quantities, house-corner lengths, and a pre-start work plan. The work plan records whether scaffolding, ESB work, or plumber work is required, with optional details for each. House notes and survey notes are edited and saved separately; survey notes appear on the appended page when sharing a PDF. Window corner lengths are calculated automatically from each window's width and two sides. Survey entries are saved per provided house. Employees cannot edit surveys.

When the house is selected in the automatic calculator, saved dimensions and corner lengths are loaded and shown read-only instead of requesting the same measurements again. The calculator continues to accept manual measurements when no surveyed house is selected. Existing user accounts and active sessions are preserved when the database is upgraded for the surveyor role.

Use **Surveyor room** on a Ready for start house to open its full survey record, pre-start work plan, and measurement form. A house must have a survey saved before it can be activated; until then, the Activate house button is disabled and explains that surveyor review is required. A surveyed perimeter and assigned employee are required before activation; the activation form can confirm or enter the perimeter and saves it with the survey. House details and history show the saved perimeter, SEAI EWI area, and a color-coded house status. SEAI insulation quantities are displayed in m². Ready cards omit notes and checklist editing; surveyors enter the planned work and optional trade details in the Surveyor room. On activation, required work starts as **Not done** and work marked **Not required** starts as **Not necessary**. Admins and surveyors can save House notes and Survey notes separately; either note field clears after save and has an **Edit note** action to load the saved text back into the field. Admins and surveyors can also attach and view PDFs for Ready for start houses. **Share PDF** prepares a copy with the original document followed by a classic, professional Survey note page that presents each note as a separate bulleted item, alongside the house and source-document details; it does not include House notes. Use the native share sheet where supported, or choose **Email PDF** or **WhatsApp PDF** to download the combined file and open the chosen app. Browsers cannot attach a local file automatically to email or WhatsApp, so attach the downloaded PDF before sending. Uploads are private to signed-in admins and surveyors, limited to 8 MB per PDF and 25 PDFs per house, and are stored in the tracker database. A document is available only while its house remains Ready for start.

Use **Annotate** beside an uploaded PDF to mark it up with a pen, highlighter, or text notes on a phone or tablet. Use the mouse wheel or trackpad to move between pages; at higher zoom, scrolling changes pages after reaching the current page's top or bottom. Choose a pen thickness and use the zoom controls to work on details; drawing renders stroke segments as you move for a more responsive experience. **Save annotated copy** stores a separate PDF alongside the original uploaded house document. In the Surveyor room, **Open & annotate** on an SEAI PDF uses the same tools; saving creates a separately named annotated PDF in the same category, leaving the original unchanged. PyMuPDF is required for local page previews and saving annotations.

Mortar / glue is estimated as the existing one bag per four insulation boards (100 mm and 60 mm combined) **plus** an area allowance of 18 bags per 100 m² of net wall area. Net wall area subtracts measured window and door areas from the wall area, and both parts are rounded up to whole bags before being added. With no openings or plinth deduction, the current examples are: 100 m² = 18 area-based + 35 existing board-based = **53 bags**; 200 m² = 36 + 70 = **106 bags**; 80 m² = 15 + 28 = **43 bags**. The calculator shows both parts and the combined total.

## Tests

```sh
py -m unittest discover -s tests
```
