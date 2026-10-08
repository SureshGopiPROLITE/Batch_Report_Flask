# Deploying SKEW Batch Report on a client PC (Windows + Docker)

Two containers run the application:

| Container | What it is | Data kept in volume |
|---|---|---|
| `batch_report_web` | Flask app + PLC monitor (gunicorn, 1 worker) | `app_logs`, `app_backups`, `app_data_files` |
| `batch_report_db` | PostgreSQL 18 | `pg_data` |

The app reaches the PLC through the PC's network. The PC just has to be able
to reach the PLC's IP address.

---

## 1. Client PC requirements (Windows)

- Windows 10/11 Pro (64-bit) with CPU virtualisation enabled in the BIOS.
  At least 8 GB RAM and 30 GB free disk space.
- **Docker Desktop** with the WSL 2 engine.
  - Settings → General → tick **Start Docker Desktop when you sign in**.
  - Docker Desktop only runs once a user is **logged in**. Set up Windows
    auto-login for the plant PC (`netplwiz`), or batches are not logged after a
    power cut until someone logs in.
  - Docker Desktop requires a **paid subscription** for companies over 250
    employees or US$10M revenue. Check the client's situation.
- Windows power plan: never sleep, never hibernate.
- A network route from the PC to the PLC:
  - Siemens S7: TCP **102**
  - Rockwell (Allen-Bradley): TCP **44818**
- Set the PC clock and time zone correctly. Batch dates, daily batch numbers
  and demo licences depend on them.

## 2. Build the release (on the development PC)

With Docker Desktop running and the `FlaskProject` environment active:

```powershell
powershell -ExecutionPolicy Bypass -File deploy\package.ps1 -Version 1.0.0
```

This produces `release\SKEW-1.0.0.zip` (about 300 MB). Copy it to the client PC
and unzip it to `C:\SKEW`:

```
C:\SKEW\
    batch-report-1.0.0-images.tar
    docker-compose.yml
    .env.example
    DEPLOY.md
    deploy\install.ps1
    deploy\db-init\01_schema.sql
    deploy\db-init\02_seed.sql
```

The zip never contains `tools\`, your private licence key, `.env`, or the
Python source files (the code is only inside the image). Add `-SkipSeed` to
reuse the existing `deploy\db-init` files instead of regenerating them from
the dev database.

## 3. Install on the client PC

With Docker Desktop running, open PowerShell in `C:\SKEW` and run:

```powershell
powershell -ExecutionPolicy Bypass -File deploy\install.ps1
```

The script:
1. Creates `.env` with random database and session passwords.
2. Records this PC's network card MAC address as `HOST_MAC` (the Machine ID).
3. Loads the images and starts SKEW.
4. Prints the web address and the **Machine ID**.

When the database volume is empty (first start only), Postgres runs
`deploy\db-init\*.sql`: it creates the tables and loads the starter
configuration (superadmin login, tag tables, materials).

### Activate the licence
1. Open `http://localhost:5000`. The **Activate licence** screen appears and
   shows the Machine ID.
2. The client sends the Machine ID to Prolite. You issue a key (section 4).
3. The client pastes the key and clicks **Activate**. The app opens and PLC
   logging starts.

### First-time configuration
1. Log in as `superadmin` and **change the password**. The seed copies the
   development password.
2. Settings: enter the PLC address (Siemens `ip,rack,slot`, for example
   `192.168.0.1,0,1`), then Connect.
3. Upload the site's PLC tag table and recipe tag table if they differ from the seed.

After that, the app reconnects to the PLC by itself after outages, and keeps
trying at startup until the PLC answers.

### Testing with a PLC simulator on the same PC
A simulator on a loopback address (`127.x.x.x`, for example Logix Emulate or
Echo on `127.0.0.10`) is **not reachable from Docker**. Inside the container,
`127.x.x.x` means the container itself. In an **Administrator** PowerShell, run:

```powershell
powershell -ExecutionPolicy Bypass -File deploy\plc_simulator_bridge.ps1 -SimulatorIp 127.0.0.10
```

Then set the Station IP in Settings to `host.docker.internal` (Siemens:
`host.docker.internal,0,1`). Remove the bridge with `-Remove`. A real PLC on
the network (for example `192.168.0.1`) needs none of this.

### Start SKEW automatically when the PC starts

The containers restart by themselves whenever Docker Desktop runs
(`restart: unless-stopped`). To get from power-on to SKEW on screen:

1. **Windows signs in by itself.** Run `netplwiz`, untick *Users must enter a
   user name and password*, and enter the account's password.
2. **Docker Desktop starts at sign-in.** In Docker Desktop, go to Settings →
   General and tick *Start Docker Desktop when you sign in*.
3. **The browser opens once SKEW is ready.** In `C:\SKEW`, run:
   ```powershell
   powershell -ExecutionPolicy Bypass -File deploy\open_skew.ps1 -Install          # default browser
   powershell -ExecutionPolicy Bypass -File deploy\open_skew.ps1 -Install -Kiosk   # full-screen Edge, no address bar
   ```
   This puts a shortcut in the Startup folder. After sign-in it waits until
   SKEW answers (up to 10 min), then opens it. Remove it with `-Uninstall`.
   To leave kiosk mode, press **Alt+F4**.

Test it by restarting the PC.

## 4. Licences (vendor)

| Type | Behaviour |
|---|---|
| **Demo** (type 0) | Full application for 30 days from the day the key is issued. Afterwards the activation screen returns and PLC logging stops. Reinstalling or deleting the database does not restart the trial, because the expiry date is inside the key. |
| **Purchased** (type 1) | No expiry. Never blocked by clock changes. |

Both types are bound to the Machine ID, so a key does not work on any other PC.
Role-based access (superadmin / admin / user / operator) works the same under both.

Issue keys on **your** PC. The private key lives in
`%USERPROFILE%\.skew_licence\private_key.pem`:

```powershell
python tools\licence_generator.py demo 54:05:db:cc:7b:1a --customer "ABC Feeds"
python tools\licence_generator.py full 54:05:db:cc:7b:1a --customer "ABC Feeds"
python tools\licence_generator.py demo 54:05:db:cc:7b:1a --days 45     # longer demo
python tools\licence_generator.py full 54:05:db:cc:7b:1a --days 365    # yearly licence
```

- **Upgrade demo → purchased:** an **admin** opens About → *Change licence key*,
  or the demo banner link, and pastes the purchased key. Before activation, or
  after a demo expires, anyone at the PC can enter a key. Once a key works,
  only an admin can replace it.
- **Back up `private_key.pem`** somewhere safe and offline. If it is lost, you
  cannot issue keys for existing installations. If it leaks, anyone can make keys.
- **The client changes the PC or network card:** issue a new key for the new
  Machine ID. `HOST_MAC` is pinned in `.env`, so updates and Docker changes
  don't affect it.

## 5. Updating to a new version

Copy the new `batch-report-X.Y.Z-images.tar` into `C:\SKEW` and run
`deploy\install.ps1` again. It loads the images, sets `APP_VERSION` and restarts.
`.env`, the licence and all data are kept.

- `db-init` scripts do **not** run again on an existing database. Apply schema
  changes as SQL migrations with `docker exec -i batch_report_db psql ...`.
- Rollback: set the old `APP_VERSION` in `.env`, then run `docker compose up -d`.
- Updating mid-batch is safe. An interrupted batch is never half-saved, and the
  PLC trigger stays set, so the batch is logged after the restart.

## 6. Backups

Settings → Database Management makes real PostgreSQL backups with `pg_dump`
(included in the image). Each backup downloads in the browser. The newest 10
are also kept in the `app_backups` volume, in `/app/Backups/Custom`.

| Button | File | Contains |
|---|---|---|
| **Full Backup** | `PLCDB2_full_<date>.dump` | The whole database |
| **Create Backup** (From/To) | `PLCDB2_<from>_to_<to>_<date>.sql` | Batches logged in the range, plus all recipes, users and settings |

**Restoring replaces the database.** Take a fresh Full Backup first, then
stop the web container so it does not log batches during the restore:

```powershell
docker compose stop web
# Full backup (.dump)
docker cp PLCDB2_full_2026-10-05_18-17-05.dump batch_report_db:/tmp/restore.dump
docker exec batch_report_db pg_restore -U postgres -d PLCDB2 --clean --if-exists --no-owner /tmp/restore.dump
# Date-range backup (.sql)
docker cp PLCDB2_2026-08-04_to_2026-08-05_2026-10-05_18-17-11.sql batch_report_db:/tmp/restore.sql
docker exec batch_report_db psql -U postgres -d PLCDB2 -v ON_ERROR_STOP=1 -q -f /tmp/restore.sql
docker compose start web
```

To look at a backup without touching production, restore it into a new
database (`docker exec batch_report_db createdb -U postgres PLCDB2_check`, then
use `-d PLCDB2_check` above).

**Also** take a database dump regularly and copy it **off the PC**:

```powershell
docker exec batch_report_db pg_dump -U postgres -Fc PLCDB2 -f /tmp/PLCDB2.dump
docker cp batch_report_db:/tmp/PLCDB2.dump "D:\SKEW-Backups\PLCDB2_$(Get-Date -Format yyyy-MM-dd).dump"
docker cp batch_report_web:/app/Backups "D:\SKEW-Backups\app-backups"
```

Schedule the dump daily with Windows Task Scheduler.

### Moving an existing site's data into Docker
```powershell
pg_dump -h localhost -p 5432 -U postgres -Fc PLCDB2 -f PLCDB2.dump    # on the old system
docker compose up -d postgres                                          # on the new PC
docker cp PLCDB2.dump batch_report_db:/tmp/PLCDB2.dump
docker exec batch_report_db pg_restore -U postgres -d PLCDB2 --clean --if-exists --no-owner /tmp/PLCDB2.dump
docker compose up -d
```

The restored database carries the old licence key. Activate a key issued for
the new PC's Machine ID.

## 7. Day-to-day operations

| Task | Command (PowerShell in `C:\SKEW`) |
|---|---|
| Status | `docker compose ps` |
| PLC / batch log | `docker exec batch_report_web tail -f /app/logs/plc_monitor.log` |
| Web server log | `docker compose logs -f web` |
| Restart the app | `docker compose restart web` |
| Stop / start everything | `docker compose stop` / `docker compose start` |

In the PLC log, each saved batch appears as `Batch N (daily M) saved`. If a batch
cannot be saved, the log says `NOT saved - trigger left set`, and the app retries
it every 5 seconds. If the licence stops being valid, the log says
`Licence no longer valid ... PLC logging stopped`.

## 8. Protecting the code on the client PC

The image is built so the client cannot read or change the application:

| Protection | Effect |
|---|---|
| Python compiled with Cython at build time | The image has **no `.py` source**, only `*.cpython-312-…so` binary modules. Docker Desktop → Files shows them, but they are not readable code. |
| Read-only container (`read_only: true`) | Nothing in the container can be changed, not even as root: Docker Desktop's *Open file editor*, `docker cp` and `docker exec` edits are all refused. Only the data volumes (logs, backups, logo) and `/tmp` are writable. |
| Non-root, no capabilities | The app runs as `appuser`, with `no-new-privileges` and all Linux capabilities dropped. |
| No vendor files in the image | `tools\` (licence generator), `deploy\` and the build script are not included. |

HTML templates and the browser JavaScript/CSS can still be read; every browser
receives them anyway. Nothing secret belongs in them.

**What remains possible for a Windows administrator on that PC:**
- **Copying the binary files:** they can't be turned back into the original source, but they are not encrypted.
- **Stopping or deleting the containers.**

Limit who can do that:
- **Separate accounts:** give operators a standard Windows account, used only for
  the browser, and keep the administrator account for Prolite. Docker Desktop
  needs its user in the `docker-users` group. The auto-login account that runs
  SKEW must be in it, so don't give that account's password to operators for
  general use.
- **Hide Docker Desktop:** close its window after starting (it keeps running in the
  tray), and turn off *Settings → General → Open Docker Dashboard when Docker
  Desktop starts*.

## 9. Never do this

- **`docker compose down -v`**: `-v` deletes the volumes, meaning all production
  data. Plain `docker compose down` is safe.
- **Changing `HOST_MAC` in `.env`** after activation. The licence stops matching.
- **Running more than one web worker or replica.** Each one starts its own PLC
  monitor, and every batch gets logged twice.
- **Publishing the database port on the LAN.** It is internal by design.
- **Shipping `tools\` or `private_key.pem` to a client.**
