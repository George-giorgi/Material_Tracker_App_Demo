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
py -m unittest discover -s tests
```
