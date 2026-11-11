# BRUSNA Material Tracker — Demo

This is an isolated demo copy of the tracker. It has its own SQLite database
at `instance/ewi.sqlite3`, uses port `8001`, and does not read or modify the
live project database. Its HTTP API and static-file routes run on FastAPI via
Uvicorn; `server.py` remains the supported launcher and applies the same host,
database, and HTTPS environment settings.

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
Supervisor, Teamlead, Plumber, Plumbing Coordinator, and Vent Coordinator accounts from
**Account → Manage accounts**.
Plumber accounts have employee-style access, including editing active-house
checklists, but cannot administer accounts or change house status. Employee
accounts remain available for compatibility.
Plumbing Coordinators see houses with an outstanding plumber-work requirement
and can assign active houses to active Supervisors who belong to Plumbing or
have Plumbing as their specialty. This assignment changes the house's
Supervisor; coordinators do not receive general house-assignment or materials
access.
Vent Coordinators see houses marked as requiring Vent work and can assign them
to active Teamleads in the Ventilation department. Coordinators only receive
access to this work queue and its team assignments.
Supervisors see only houses assigned to their account and can set each house's
delivery person and team from their dashboard. The dashboard includes search
and filters for active, closed, and unassigned houses, along with a collapsible
view of active Teamleads who report to that Supervisor and their non-login
roster members. Houses assigned to an EWI Supervisor through the Assign
workflow can be assigned to one of their reporting EWI Teamleads for EWI work.
Active EWI Teamleads are also selectable as delivery people for the EWI
Supervisor's assigned houses; this does not grant Teamleads delivery-account
permissions.
The Teamlead sees those houses in an EWI work queue and can mark the assigned
EWI work complete without closing the house. Teamlead cards summarize open and
completed EWI assignments.
Supervisor specialties are stored on supervisor accounts and can be maintained
in **Account → Manage accounts**. This describes the supervisor's trade; house
assignment access continues to follow the existing supervisor permissions.
The Teams view includes a non-login roster for each active Teamlead. Admins can
name each Teamlead's team when creating or editing the account. Teamleads can
manage their own roster; Supervisors can manage rosters for Teamleads who report
to them. Administrators maintain department membership and reporting lines from
**Account → Manage accounts**.
User records include optional `work_phone`, `home_phone`, `company_email`, and
`private_email` fields for supervisors, Teamleads, and Surveyors. They remain
empty until contact details are provided. The company email is named
“Company Email” on contact sheets.
Give each person their own username and PIN; do not share PINs. Users can
change their PIN from **Account**; sessions expire after 12 hours. Five
incorrect PIN attempts lock that username for 15 minutes. A short PIN is less
secure than a password, so only use this sign-in over HTTPS on a trusted
network. If a tracker database already contains an older password-based
account, its existing password still works in the PIN field; change it to a
6-digit PIN after signing in.

The database has editable Departments, user-to-department memberships, and
manager-to-report relationships. Each account must belong to at least one
department; a user may belong to more than one. Supervisors can be department
members without being assigned as that department's manager. Administrators
can add departments and custom roles, assign a manager when the users share a
department, and manage which access profiles belong to each role. Reporting
lines support Admin -> Assign, Supervisor -> Teamlead, and Teamlead ->
Employee/Plumber; departments can exist without a Supervisor.
The built-in role catalog includes Admin, Assign, Boss, Surveyor, Warehouse,
Delivery, Supervisor, Teamlead, Plumber, Plumbing Coordinator, Vent Coordinator,
and Employee.
Role permissions are
server-enforced access profiles (such as Surveyor, Supervisor, or Teamlead),
and a custom role can combine profiles without changing the user or role
database schema. Admin's `*` permission grants unrestricted access. Users with
no active access profile cannot use application APIs.

The SEAI Red dataset is initialized with Administration, External Wall
Insulation, Surveyors, Delivery, Ventilation, Attic, and Plumbing departments.
The existing EWI Supervisor Karol is linked to the four EWI Teamleads shown in
the organization chart and is also listed in Surveyors / Quality Check, as in
the drawing. Existing roster names remain non-login team members assigned to
their Teamlead. Attic and Plumbing departments are ready for additional
structure as it is provided.

## Run the separate SEAI Red dataset

The separate `instance\ewi.seai-red.sqlite3` database is an isolated SEAI demo
copy with ten fictional `DEMO-FIC` houses. Run
`seed_seai_measure_demo.py` with `EWI_DB_PATH` explicitly set to this database
to create synthetic application measure rows in `seai_applications` and
`seai_measures`, as well as a synthetic Variance section. The values follow the
SEAI sections' field shapes but are fabricated for layout and testing; they
are not copied from portal applicants or live records. The app labels these
details as demo data in **View SEAI details**. This database is separate from
`instance\ewi.sqlite3`.

The database retains the demo material catalog and stock data, which are not
real inventory. A pre-change backup is kept beside the database in `instance\`.

### Start with the shared schema and role permissions

`database\ewi-seai-red-template.sqlite3` is a clean starter database for a new
checkout. It contains the current table structure, built-in roles, permission
catalog, and role-to-permission assignments, but no users, sessions, houses,
stock, or SEAI records. To use it on a home computer, copy it to
`instance\ewi.seai-red.sqlite3` in a fresh checkout, then start the app using
the PowerShell command below. Create the first administrator locally and add
fake users as needed. Do not overwrite an existing database; this template
does not contain the live database's records.

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

## Remote access with Cloudflare Quick Tunnel

The SEAI Red setup uses port `8011` and listens on `127.0.0.1`. Cloudflare
Tunnel can publish it without opening a port on the router or making the app
listen on the local network. The host PC must stay on, with both the app and
the tunnel running.

Install `cloudflared` on Windows (for example, from PowerShell with
`winget install --id Cloudflare.cloudflared -e`). Start the app using the
Quick Start command above. In a second PowerShell window, from the project
folder, run:

```powershell
cloudflared tunnel --url http://127.0.0.1:8011
```

Cloudflare prints a temporary `https://…trycloudflare.com` address. Share that
HTTPS address with the intended users; keep the `cloudflared` window open.
You can also run the app and tunnel from VS Code using the **Run SEAI Red app
(port 8011)** and **Run Cloudflare Quick Tunnel (port 8011)** tasks. Local use
continues at [http://127.0.0.1:8011](http://127.0.0.1:8011). A Quick Tunnel
address changes when the tunnel restarts and is for testing, not a permanent
service.

The Quick Tunnel address is reachable from the public internet by anyone who
knows it. The app login remains required, but this setup does not add
Cloudflare Access. Use individual accounts and unique PINs, and share the URL
only with authorized users. Because this database contains sensitive
applicant, property, contact, and notes data, configure Cloudflare Access and
a named Cloudflare Tunnel before using it for routine work. Do not use a
Quick Tunnel for sensitive production data.

Employees can view tracker records and record material movements against
existing houses. They cannot add materials, count or adjust stock, import
workbooks, create houses, or finish/reopen houses. These restrictions are
enforced by the server, not only by hiding buttons in the interface.
Admins, employees, and Plumbers can update scaffolding, ESB-work, and plumber-work
completion directly on Active house cards: tick an item to mark it done (green),
or untick it to return it to not done. Work marked not required by the surveyor
is shown as not necessary. Ready-for-start cards keep notes and checklist
editing out of the card; use the Surveyor room for the pre-start work plan.
House detail and movement history remain available to administrators only.
The Teams tab also lists employees assigned to houses at activation and shows
their open and closed house counts; selecting a count opens those houses.

## Synthetic house preview

After signing in as an administrator or surveyor, open
`http://127.0.0.1:8001/oldest-house-test` to view a separate, read-only sample
application with synthetic property details, vector illustrations, and six
fabricated PDFs. The preview does not import or change tracker house data.
Employee accounts cannot access this page or its documents. Recreate the sample
assets and the PDF bundle used by the demo seeder with:

```powershell
py tools\create_synthetic_demo_assets.py
```

The generator writes only fictional data. Keep real applicant records, photos,
PDFs, contact sheets, and organization charts out of GitHub.

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

For an individual pytest test, install the test dependencies and run, for
example:

```powershell
py -m pip install -r requirements-test.txt
py -m pytest tests/test_server.py -k ewi_assignment
```

For the end-to-end browser test, install Chromium once, then run:

```powershell
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

Administrators can add employee or surveyor accounts in **Account ? Manage employees**. Surveyors start at a focused dashboard showing only houses assigned to their account that are marked **Ready for start**, with workload counts, search, filters for surveys needed or completed, and refresh; **Start survey** or **Review survey** opens the Surveyor room for that house. The room shows the assigned house's property details, house notes, and captured SEAI application as reference information. Surveyors cannot edit house notes; only **Survey Qty including Variation** is editable in the SEAI record. House cards show reference fields and insulation quantities from each house's retrieved SEAI application only; synthetic demo records and missing quantities are not presented as verified SEAI information. The dashboard includes the BRUSNA logo and refreshes assigned houses without requesting restricted stock or movement data. Signed-in tabs check lightweight change revisions and reload only the currently relevant view; updates to other views are kept pending until those views are opened, and polling pauses while the browser tab is hidden. The Surveyor room has section shortcuts and a dark-mode layout for house details, notes, work planning, documents, and measurements. Surveyors can add survey notes, window and door sizes with quantities, house-corner lengths, and a pre-start work plan. The work plan records whether scaffolding, ESB work, or plumber work is required, with optional details for each. Survey and house notes are stored separately; survey notes appear on the appended page when sharing a PDF. Administrators can maintain house notes. Window corner lengths are calculated automatically from each window's width and two sides. Survey entries are saved per provided house. Employees cannot edit surveys.

When the house is selected in the automatic calculator, saved dimensions and corner lengths are loaded and shown read-only instead of requesting the same measurements again. The calculator continues to accept manual measurements when no surveyed house is selected. Existing user accounts and active sessions are preserved when the database is upgraded for the surveyor role.

Use **Surveyor room** on a Ready for start house to open its full survey record, pre-start work plan, and measurement form. **Survey notes** has its own editable section, separate from the assigned house reference notes; saved survey notes are appended to shared PDFs on a separate final page. Saving survey notes alone does not mark a survey complete; completion requires a valid saved perimeter. A house must have a completed survey before it can be activated; until then, the Activate house button is disabled and explains that surveyor review is required. A surveyed perimeter and assigned employee are required before activation; the activation form can confirm or enter the perimeter and saves it with the survey. Admins and Assign users can assign an active Surveyor to a not-started house beside **Mark ready for start** or change the assignment on a Ready for start house. The not-started card shows whether a Surveyor is assigned and their name; on Ready for start cards, the assigned Surveyor’s name and assignment time appear beside the **Marked ready to start** audit, with **Not surveyed yet** or **Survey completed** below the Ready for start status. After the Surveyor records a valid survey and perimeter, **Activate house** becomes available; clicking it opens a prompt to choose an active Supervisor, and confirming assigns that Supervisor and activates the house. The Supervisor is recorded as the active house employee. Assign users can activate houses without receiving materials or calculator access. House details and history show the saved perimeter, SEAI EWI area, and a color-coded house status. SEAI insulation quantities are displayed in m². Ready cards omit notes and checklist editing; surveyors enter the planned work and optional trade details in the Surveyor room. On activation, required work starts as **Not done** and work marked **Not required** starts as **Not necessary**. Admins and surveyors can edit House notes separately from the dedicated **Survey notes** section; the saved survey note is appended to shared PDFs on its own final page. Admins and surveyors can also attach and view PDFs for Ready for start houses. **Share PDF** prepares a copy with the original document followed by a classic, professional Survey note page that presents each note as a separate bulleted item, alongside the house and source-document details; it does not include House notes. Use the native share sheet where supported, or choose **Email PDF** or **WhatsApp PDF** to download the combined file and open the chosen app. Browsers cannot attach a local file automatically to email or WhatsApp, so attach the downloaded PDF before sending. Uploads are private to signed-in admins and surveyors, limited to 8 MB per PDF and 25 PDFs per house, and are stored in the tracker database. A document is available only while its house remains Ready for start.

Use **Annotate** beside an uploaded PDF to mark it up with a pen, highlighter, or text notes on a phone or tablet. The PDF opens at 175% zoom for a closer initial view; use **Fit** or the zoom controls to adjust it. When placing a text note, choose Small, Medium, Large, Extra large, or XXL before tapping the page. Tap a placed note to select it, then use the selected-note size slider or drag its large green corner handle to resize and wrap the text. Keep using the mouse wheel or trackpad to move between pages; at higher zoom, scrolling changes pages after reaching the current page's top or bottom. The page panel shows reading progress and has previous/next buttons; to jump directly, enter a page number and select **Go**. Choose a pen thickness and use the zoom controls to work on details; drawing renders stroke segments as you move for a more responsive experience. **Save annotated copy** stores a separate PDF alongside the original uploaded house document. In the Surveyor room, **Open & annotate** on an SEAI or test PDF adds the annotated page to **Review selected pages** without saving a separate document. Tap a placed Vent icon to select it; its high-contrast resize frame and handles are shown while selected. Drag a corner handle to resize, the icon to move, or the top handle to rotate. Review the combined PDF, then choose **Save selected PDFs to house** to make the final selection available in the room and to the assigned Vent team; the source remains unchanged. PyMuPDF is required for local page previews and saving annotations.

Mortar / glue is estimated as the existing one bag per four insulation boards (100 mm and 60 mm combined) **plus** an area allowance of 18 bags per 100 m² of net wall area. Net wall area subtracts measured window and door areas from the wall area, and both parts are rounded up to whole bags before being added. With no openings or plinth deduction, the current examples are: 100 m² = 18 area-based + 35 existing board-based = **53 bags**; 200 m² = 36 + 70 = **106 bags**; 80 m² = 15 + 28 = **43 bags**. The calculator shows both parts and the combined total.

## Tests

```sh
py -m unittest discover -s tests
```
