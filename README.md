# Эрдэнэт Ажил

A web app for a daily-labor matching office in Erdenet. It runs as one Flask application with Mongolian-language pages.

## Setup (Windows PowerShell)

```powershell
# One-time: allow local scripts such as the venv activator (current user only)
Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned

python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env      # then put a real SECRET_KEY in .env
```

If you don't want to change the execution policy, skip activation and call the venv's Python directly:
`.\.venv\Scripts\python.exe -m pytest`.

## Database

```powershell
flask --app app db upgrade        # create/update tables in instance/erdenet_ajil.sqlite3
flask --app app create-admin      # asks for phone, name and password (hidden)
```

After changing `app/models.py`:

```powershell
flask --app app db migrate -m "describe the change"   # then review the file in migrations/versions/
flask --app app db upgrade
```

## Office phone and map

Set the office phone shown on the public job board in `.env`:

```
OFFICE_PHONE=7035xxxx
```

8 digits, optionally with `+976`. Restart the server after changing it. Without it the board shows
"not configured" and admins see a warning banner.

The map uses Leaflet with OpenStreetMap's public tiles (free, no API key). OSM's tile policy only
allows light use; for a busy production site set `MAP_TILE_URL` to a tile provider you're allowed to use.

## How work is allocated (Milestone 8)

1. The office creates and **publishes** a listing (Зарууд). Only published, open listings take applications.
2. A worker browses the public board, presses **«Ажилд хүсэлт илгээх»**, logs in or registers, and applies.
   A pending application does not hold a place.
3. Office staff review applications at **Ажилтны хүсэлт** (`/admin/applications`). «Баталгаажуулах» (accept)
   creates the assignment immediately, after rechecking places, the worker's availability and overlapping jobs.
4. The accepted worker sees the exact address, supervisor contact and instructions on their job page.
5. The office records attendance. A worker, the employer or the office can report a disagreement; while one is
   open the job can't be completed. Completing the job freezes wages and commission for the finance pages.

Old phone referrals (Илгээлт) still work and still hold places.

## Run

```powershell
flask --app app run --debug
```

## Test

```powershell
python -m pytest
```
