# Push UNIWORK to GitHub and deploy it to Render

This guide deploys the project source in this repository. GitHub and Render account authentication must be completed by the project owner; do not share passwords, personal access tokens or API keys in chat.

## 1. Prepare the folder

1. Extract the release ZIP. Open a terminal in the folder that contains `README.md`, `render.yaml`, `backend/` and `frontend/`.
2. Check that `.env` files, SQLite databases, `backend/instance/`, `node_modules/` and virtual environments are not included. The `.gitignore` excludes the local database and `.env` files. Keep `backend/.env.example`; it contains placeholders only.
3. Run local tests before publishing:

```powershell
cd backend
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements-production.txt
python -m unittest discover -s tests -v
python run.py
```

Open `http://127.0.0.1:5000` and test the major flows. Do not claim end-to-end verification until the test suite and browser workflows pass.

## 2. Create a GitHub repository and push

1. Sign in to GitHub and create a new repository named `UNIWORK-Vendor-Invoice`. For the simplest first push, create it empty (do not pre-create a README, license or `.gitignore`). Choose Public only if your instructor/evaluation requires public access; otherwise choose Private and verify that the evaluator can access it.
2. In PowerShell at the project root, run the following commands, replacing the URL with the actual repository URL displayed by GitHub:

```powershell
git init -b main
git status --short
git add .
git status --short
git commit -m "Prepare UNIWORK v5 project submission"
git remote add origin https://github.com/YOUR_USERNAME/UNIWORK-Vendor-Invoice.git
git push -u origin main
```

If the folder already has a Git remote, inspect it with `git remote -v` and use `git remote set-url origin https://github.com/YOUR_USERNAME/UNIWORK-Vendor-Invoice.git` rather than adding a second `origin`. Authenticate using Git Credential Manager or GitHub CLI. Never commit a token or password.

## 3. Deploy on Render with the Blueprint

1. Sign in to Render. Choose **New → Blueprint** and connect the GitHub repository containing `render.yaml`.
2. Review the service name and configuration before creating it. The Blueprint uses `backend/requirements-production.txt`, starts Gunicorn from `backend/`, and checks `/health`.
3. Set `ADMIN_PASSWORD` to a unique strong password (at least 12 characters). Set `VENDOR_DATA_KEY` to a newly generated Fernet key. Generate it locally after installing the declared dependencies:

```powershell
cd backend
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Paste the output into Render's secret field only. Do not place it in Git. A generated `SECRET_KEY` is configured in the Blueprint. If your application is not using supplier bank-data features during the demo, keep the key private anyway; configuring it enables the encryption feature.

4. When deployment completes, open the service's public URL and check `https://YOUR-SERVICE.onrender.com/health`. It should return `{"status":"ok"}`. Then test login, dashboard, vendors, purchase orders, receipts, stock, invoice matching, GST/ITC review and reports. Review Render logs if deployment fails.

### Deploying to the existing Render service instead

If you need to keep an existing `uniwork-vendor-invoice.onrender.com` URL, do not create a second service unless intended. In the existing Render service settings, connect the new GitHub repository and set:

- **Build command:** `pip install -r backend/requirements-production.txt`
- **Start command:** `cd backend && gunicorn --workers 1 --bind 0.0.0.0:$PORT app:app`
- **Health check path:** `/health`

Set these environment variables in Render's dashboard: `FLASK_ENV=production`, `SECRET_KEY` (unique random value, at least 32 characters), `ADMIN_USERNAME=admin` (or your chosen administrator username), `ADMIN_PASSWORD` (unique strong password; at least 12 characters and not the demo password), `COOKIE_SECURE=1`, `SHOW_DEMO_CREDENTIALS=0`, and `VENDOR_DATA_KEY` (generated Fernet key). Save the settings, deploy the selected branch and confirm the live URL.

## 4. Important SQLite persistence warning

The Blueprint is configured for a free demonstration instance. Render's free filesystem is ephemeral; writes to the SQLite database can be lost on restart, redeploy or spin-down. This is acceptable only for a disposable demo with synthetic data. For an evaluation that must preserve user changes, use a paid Render web service with a persistent disk mounted at `/var/data`, set `UNIWORK_DB=/var/data/uniwork.sqlite3`, and test backups. An eventual production deployment should assess a managed database migration; this app currently uses SQLite.

## 5. Upload a release asset for the required “Binary / APK / Release File URL” field

UNIWORK is a Python web application, not an Android app, so an APK is not applicable. Use the source release ZIP as the release artifact. After the code is pushed:

1. On GitHub, open **Releases → Draft a new release**.
2. Create tag `v5.0.0`, set title `UNIWORK v5.0.0`, and add the release notes from `RELEASE_NOTES_v5.0.0.md`.
3. Attach `UNIWORK-Source-Release-v5.0.0.zip` from the release package (download this ZIP or make a copy of the submitted source ZIP with that filename).
4. Publish the release, then copy the asset's download link. A typical URL shape is `https://github.com/YOUR_USERNAME/UNIWORK-Vendor-Invoice/releases/download/v5.0.0/UNIWORK-Source-Release-v5.0.0.zip`; use the actual URL displayed by GitHub, not this placeholder.

## 6. Final submission freeze checklist

Before confirming an immutable/final submission, open each link in a signed-out/incognito browser (or otherwise confirm the evaluator has access):

- Source code / forked repository URL
- Public live Render URL (not `127.0.0.1`, `localhost`, or a private LAN IP)
- `/health` responds successfully
- GitHub release asset download URL
- “How it works & technical architecture” write-up pasted into the required field
- PDF documentation and PPTX are uploaded and open correctly
- Part 4 code ZIP is uploaded

Capture the final repository URL, release asset URL and live URL in your submission record. Do not click the final immutable submit/freeze button until every required link has been checked.
