# BRUSNA Material Tracker: Windows 11 Test and Deployment Tutorial

This guide covers a local demo on a Windows 11 laptop, an optional temporary
Cloudflare Quick Tunnel test, and the safer long-term deployment shape. The
application uses Python, FastAPI/Uvicorn, and SQLite. Node.js is not required
to run it.

> **Quick Tunnel warning:** A `trycloudflare.com` Quick Tunnel is public,
> temporary, and does not provide Cloudflare Access or MFA. Anyone who gets
> its URL can reach the tracker sign-in page. Use only a disposable database
> with fictional demo records. Do not use real employee, customer, applicant,
> property, or inventory data with a Quick Tunnel.

## 1. Get the project onto the Windows laptop

Install Git and Python 3.12 (64-bit) if they are not already installed. Once
this tutorial has been pushed to GitHub, clone the branch containing it:

```powershell
New-Item -ItemType Directory -Force C:\Brusna | Out-Null
git clone --branch feature/fastAPI-copy0.1 https://github.com/George-giorgi/Material_Tracker_App_Demo.git C:\Brusna\MaterialTracker
Set-Location C:\Brusna\MaterialTracker
```

If you already cloned the repository, fetch and switch to the branch instead:

```powershell
Set-Location C:\Brusna\MaterialTracker
git fetch origin
git switch feature/fastAPI-copy0.1
git pull
```

Do not copy the Mac `.venv` directory. Create a separate Python environment
on Windows.

## 2. Install dependencies and create a disposable demo database

Run these commands in PowerShell from the project directory. Pick a new test
database filename if the example already exists; the checks intentionally stop
instead of overwriting an existing file.

```powershell
Set-Location C:\Brusna\MaterialTracker

py -3.12 --version
if (-not (Test-Path .\.venv\Scripts\python.exe)) {
  py -3.12 -m venv .venv
}
.\.venv\Scripts\python.exe -m pip install -r requirements.txt

New-Item -ItemType Directory -Force C:\BrusnaData | Out-Null
$db = 'C:\BrusnaData\ewi.quick-test.sqlite3'
if (Test-Path -LiteralPath $db) {
  throw "This test database already exists. Choose a different filename."
}

$env:EWI_DB_PATH = $db
$env:EWI_HOST = '127.0.0.1'
$env:EWI_PORT = '8001'

.\.venv\Scripts\python.exe seed_demo.py
.\.venv\Scripts\python.exe server.py
```

Open [http://127.0.0.1:8001](http://127.0.0.1:8001). Create an administrator
account when prompted, then confirm you can sign in. The demo seeder adds
fictional houses, materials, and movements. It refuses to seed a database
that already contains materials.

Keep the app running for local testing. Press **Ctrl+C** in its PowerShell
window when you need to stop it.

### Optional tests

In another PowerShell window, from the project directory:

```powershell
.\.venv\Scripts\python.exe -m py_compile server.py
.\.venv\Scripts\python.exe -m unittest tests.test_server tests.test_seai_measure_demo
```

The test database uses SQLite and does not require a separate database server.
JavaScript/navigation tests also require Node.js; Node.js is not needed to run
the application.

## 3. Optional temporary Cloudflare Quick Tunnel

First install `cloudflared` using Cloudflare's official
[Windows download instructions](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/downloads/).
Open a new PowerShell window and verify it is available:

```powershell
cloudflared --version
```

Leave the application running locally in the first PowerShell window. In the
second window, start the tunnel:

```powershell
cloudflared tunnel --url http://127.0.0.1:8001
```

Copy the **current** `https://....trycloudflare.com` URL printed in that
window. Leave the tunnel process running. If the command is not recognized,
finish installing Cloudflare's Windows package, open a fresh PowerShell
window, and run `cloudflared --version` again.

The app must allow the public HTTPS origin before browser POST requests such as
login, account setup, and sign-out will work. In the app's PowerShell window,
press **Ctrl+C** to stop it, then restart it with the exact URL from the
current tunnel:

```powershell
Set-Location C:\Brusna\MaterialTracker
$env:EWI_DB_PATH = 'C:\BrusnaData\ewi.quick-test.sqlite3'
$env:EWI_HOST = '127.0.0.1'
$env:EWI_PORT = '8001'
$env:EWI_COOKIE_SECURE = 'true'
$env:EWI_TRUSTED_ORIGINS = 'https://REPLACE-WITH-CURRENT-NAME.trycloudflare.com'
.\.venv\Scripts\python.exe server.py
```

Replace the example origin with the exact hostname Cloudflare printed. Use
only the origin (scheme and host), with no path or trailing slash. Open that
HTTPS URL and sign in using the administrator created during local setup.
Keep both PowerShell windows open while using the tunnel. Stop both processes
with **Ctrl+C** when finished. A new Quick Tunnel usually has a different URL;
update `EWI_TRUSTED_ORIGINS` and restart the app for the new URL.

## 4. Long-term Windows host and Cloudflare Tunnel

Do not use a Quick Tunnel for employee access or real business data. For a
permanent deployment:

1. Use a domain you own and configure a named Cloudflare Tunnel from the
   Cloudflare Zero Trust dashboard.
2. Put a stable application copy in a directory such as
   `C:\Brusna\MaterialTracker`. Put the production SQLite database in a
   restricted local directory such as `C:\BrusnaData\ewi.sqlite3`.
3. Use a single application process bound to `127.0.0.1`, and configure the
   tunnel origin as `http://127.0.0.1:8001`. Do not expose port 8001 publicly
   or bind the app to `0.0.0.0` for this arrangement.
4. Configure Cloudflare Access to allow only intended employees and require
   MFA. A tunnel alone is not employee authentication.
5. Set machine-level app configuration to the exact public HTTPS hostname.
   Run PowerShell as Administrator and replace the example hostname:

   ```powershell
   [Environment]::SetEnvironmentVariable('EWI_DB_PATH', 'C:\BrusnaData\ewi.sqlite3', 'Machine')
   [Environment]::SetEnvironmentVariable('EWI_HOST', '127.0.0.1', 'Machine')
   [Environment]::SetEnvironmentVariable('EWI_PORT', '8001', 'Machine')
   [Environment]::SetEnvironmentVariable('EWI_COOKIE_SECURE', 'true', 'Machine')
   [Environment]::SetEnvironmentVariable('EWI_TRUSTED_ORIGINS', 'https://tracker.example.com', 'Machine')
   ```

6. Configure the app and `cloudflared` to start after Windows restarts. Use
   Windows Task Scheduler for the app: trigger **At startup**, run under a
   dedicated least-privilege account, set the program to
   `C:\Brusna\MaterialTracker\.venv\Scripts\python.exe`, the argument to
   `C:\Brusna\MaterialTracker\server.py`, and **Start in** to
   `C:\Brusna\MaterialTracker`. Configure restart-on-failure and verify after
   reboot. Install `cloudflared` as a Windows service using Cloudflare's
   instructions. Restart services after changing machine environment values.
7. Keep the SQLite database on a local disk, not a network share or synced
   folder. SQLite permits only one writer at a time; the app waits up to 30
   seconds for brief lock contention, but do not run multiple app processes
   against one database.
8. Back up the database regularly. House and SEAI PDF uploads are stored
   inside SQLite, so backing up the database preserves the uploaded documents.
   Stop the app before a file-copy backup and periodically test a restore to a
   separate path. Restrict database and backup access to the service account
   and administrators.

Do not put tunnel tokens, private keys, passwords, or other credentials in
Git, this tutorial, task arguments, or shared scripts. The application uses
six-digit PINs, so require Cloudflare Access with MFA and keep the app behind
the tunnel. Do not use demo seed scripts or a demo database for production.
