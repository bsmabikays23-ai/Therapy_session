# Therapy_session

Simple Flask app for a therapeutic chat and journaling demo.

Quick start (Windows PowerShell):

```powershell
python -m venv .venv
.\.venv\Scripts\Activate
python -m pip install --upgrade pip
pip install -r requirements.txt
python app.py
```

The app uses a local SQLite DB by default (`instance/therapy.db`) and an optional `HF_API_KEY` for Hugging Face model access.
