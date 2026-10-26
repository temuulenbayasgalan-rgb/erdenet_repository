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

## Run

```powershell
flask --app app run --debug
```

## Test

```powershell
python -m pytest
```
