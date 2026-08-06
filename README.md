# Lead pipeline (dental agencies)

Finds dental marketing agency founders, scrapes a site signal, appends rows to Google Sheets, sends first-touch + follow-ups via Gmail API (HTTPS), and syncs replies back into the CRM.

Cold-email openers are written by the Cursor agent (no external LLM required).

## 1. Setup

### 1.1 Install

```bash
pip install -r requirements.txt
cp .env.example .env
```

Fill `.env` (never commit it — already in `.gitignore`).

### 1.2 Apollo

Either:

- set `APOLLO_KEY` (Master / enrichment scopes for `people/bulk_match`), **or**
- export CSV from Apollo UI (Search → People → Export) → save as `data/leads.csv`

Without `APOLLO_KEY`, research falls back to the CSV.

### 1.3 Google Sheets

1. Create a service account in Google Cloud  
2. Enable **Google Sheets API** and **Google Drive API**  
3. Download JSON → save as `service_account.json` in the project root  
4. Share spreadsheet **Pilot CRM** with the service account `client_email` as **Editor**  
5. Worksheet **Agencies** is created/used by the pipeline (`Лист1` is never modified)

Optional env: `SHEET_NAME`, `WORKSHEET_NAME`, or paste the full JSON into `GOOGLE_SERVICE_ACCOUNT` for CI.

### 1.4 Gmail send via REST API (OAuth2) — required for `send` / `followup`

Outbound SMTP (465/587) is often blocked by ISPs. Sending uses the **Gmail API over HTTPS (443)** instead.

1. In [Google Cloud Console](https://console.cloud.google.com/) (same project is fine):
   - **APIs & Services → Library** → enable **Gmail API**
   - **APIs & Services → Credentials → Create Credentials → OAuth client ID**
   - Application type: **Desktop app**
   - Download the JSON → save as `client_secrets.json` in the project root  
     (path override: `GMAIL_CLIENT_SECRETS` in `.env`)
2. Set `GMAIL_EMAIL` in `.env` to the mailbox you will send from  
   (today: DentalMeet inbox for warm/client mail; cold outreach should later
   move to a separate domain — keep the primary domain for replies/clients)  

3. Run the one-time browser consent flow:

```bash
python -m pipeline.setup_gmail_oauth
```

4. Sign in as that Gmail account in the browser and allow `gmail.send`  
5. Script writes `gmail_token.json` (gitignored). Later runs **refresh the token automatically** — you normally do this setup once.

Test:

```bash
# print only
$env:DRY_RUN='true'; python -m pipeline.main send --limit 1

# real send over HTTPS
$env:DRY_RUN='false'; python -m pipeline.main send --limit 1
```

### 1.5 Gmail App Password + IMAP — required for `replies`

Reply sync still uses IMAP (not the Gmail send API):

1. Enable **2-Step Verification** on `GMAIL_EMAIL`  
2. Create an [App password](https://myaccount.google.com/apppasswords) (any name, e.g. `DentalMeet IMAP`)  
3. Put the 16-character value in `GMAIL_APP_PASSWORD` (spaces ok)  
4. Enable IMAP: Gmail → ⚙️ **Settings** → **See all settings** → **Forwarding and POP/IMAP** → **Enable IMAP** → Save

### 1.6 Calendly

Set `CALENDLY_URL` in `.env` (default: `https://calendly.com/dentalmeet-agency/new-meeting/`).

### 1.7 Google Form responses (for `check_forms`)

1. Open the form → **Responses** → green Sheets icon → create/open the responses spreadsheet  
2. Share that spreadsheet with the service account as **Editor**:  
   `pipeline@dentalmeet-pipeline.iam.gserviceaccount.com`  
3. Set in `.env`:  
   `FORM_RESPONSES_SPREADSHEET=<exact spreadsheet title>`  
   `FORM_RESPONSES_WORKSHEET=Form Responses 1`

### 1.8 Gemini (optional — smarter reply drafts)

Used by `pipeline/ai_reply_assistant.py` → `draft_reply`.

1. Go to [Google AI Studio](https://aistudio.google.com/apikey) (same Google account is fine)  
2. **Create API key** → copy it  
3. Put in `.env`: `GEMINI_KEY=...`  
4. Test:  
   `python -m pipeline.main draft_reply someone@agency.com --body "How long until results?"`

Without `GEMINI_KEY`, drafts use a built-in FAQ template (still works).

### 1.9 Delivery (stage 3) — client Workspace OAuth

```bash
# Terminal A — callback server (add redirect URI in GCP OAuth client first)
python -m delivery.connect_workspace start_server

# Terminal B — email connect link to client
python -m delivery.connect_workspace send_link client@agency.com --name John

# After client connects + brief is in Clients:
python -m delivery.research_dental build_list client@agency.com --limit 50
python -m delivery.send_as_client run client@agency.com --limit 10

# Payment flow (stages 4–10)
python -m delivery.track_meetings fetch client@agency.com
python -m delivery.track_meetings list client@agency.com
python -m delivery.track_meetings dashboard client@agency.com
python -m delivery.qualify_meetings check client@agency.com
python -m delivery.qualify_meetings report client@agency.com
python -m delivery.billing invoice client@agency.com
python -m delivery.billing send client@agency.com
python -m delivery.payment_tracker verify TXID
python -m delivery.payment_tracker mark_paid client@agency.com TXID
python -m delivery.payment_tracker check_overdue

# Pilot vs Repeat
python -m delivery.client_tracker check client@agency.com
python -m delivery.client_tracker complete_pilot client@agency.com

python -m delivery.weekly_report send client@agency.com
```

**Form (manual):** add question *Your Calendly API token* → column `Calendly API Token` in Clients.

Agreement template: [`docs/pilot_agreement.md`](docs/pilot_agreement.md) (copy into Google Docs).

**Payment model:** Pilot = $0 upfront, $150/qualified meeting, max $750, pay after.  
Repeat = $300 upfront + $150/meeting (or $1000 flat / 7).

In GCP OAuth client, add authorized redirect URI:  
`http://localhost:8000/oauth/callback`  
(For Desktop clients you may need a **Web application** OAuth client with that redirect.)

## 2. CLI commands

```bash
# Research → Sheets (default if no subcommand)
python -m pipeline.main
python -m pipeline.main research
python -m pipeline.main research --dry-run          # print leads+signals JSON only
python -m pipeline.main research --from-csv         # ignore APOLLO_KEY
python -m pipeline.main research --page 2

# First-touch send (unsent, Verified=yes|manual) — Gmail API / HTTPS
python -m pipeline.main send
python -m pipeline.main send --limit 5
python -m pipeline.main send --limit 10

# Follow-ups
python -m pipeline.main followup 1   # Sent 3+ days ago, no reply, no FU1 yet
python -m pipeline.main followup 2   # Sent 7+ days ago, no reply, after FU1

# Reply check (IMAP → Replied / Reply type; interested → async onboarding)
python -m pipeline.main replies
python -m pipeline.main replies --days 7

# Ingest Google Form onboarding → Clients worksheet
python -m pipeline.main check_forms

# One-time Gmail OAuth
python -m pipeline.setup_gmail_oauth
```

### DRY_RUN testing

In `.env`:

```bash
DRY_RUN=true
```

Then `send` / `followup` print emails to the console and **do not** call the Gmail API or mark `Sent` / Notes.  
Unset or set `DRY_RUN=false` for real sends.

```bash
# PowerShell one-off
$env:DRY_RUN='true'; python -m pipeline.main send --limit 2
```

## 3. Recommended daily limits

| Action | Soft cap in code | Recommendation |
|--------|------------------|----------------|
| First-touch (`send`) | max **12** / run | start with `--limit 5` |
| Follow-ups | max **10** / wave | keep default |
| **Total from a new Gmail** | — | **do not exceed ~25 emails/day** |

Warm up gradually (e.g. 5 → 10 → 15 over a week). Spikes from a cold inbox get spam-foldered or restricted by Google.

## 4. GitHub Actions schedule

Workflow: `.github/workflows/pipeline.yml`

| Job | Cron (UTC) | Command |
|-----|------------|---------|
| `research` | `0 8 * * *` daily | `python -m pipeline.main research` |
| `send` | `0 14 * * 1-5` weekdays | `python -m pipeline.main send --limit 5` |
| `followup1` | `0 15 * * 1-5` weekdays | `python -m pipeline.main followup 1` |
| `followup2` | `0 16 * * 1-5` weekdays | `python -m pipeline.main followup 2` |
| `replies` | `0 */6 * * *` every 6h | `python -m pipeline.main replies` |
| `check_forms` | `0 */2 * * *` every 2h | `python -m pipeline.main check_forms` |

Manual run: Actions → **lead-pipeline** → **Run workflow** → pick job (`research` / `send` / … / `all`).

### Repo secrets to add

`APOLLO_KEY`, `GEMINI_KEY` (optional), `GOOGLE_SERVICE_ACCOUNT` (full JSON string), `SHEET_NAME`, `WORKSHEET_NAME`, `GMAIL_EMAIL`, `GMAIL_APP_PASSWORD`, `CALENDLY_URL`, `GMAIL_TOKEN_JSON` (contents of `gmail_token.json` after local OAuth — needed for send/followup jobs)

Keep secrets out of git. Prefer GitHub Secrets over committing `.env` / `gmail_token.json`.

## 5. Troubleshooting

| Error | Fix |
|-------|-----|
| `Missing gmail_token.json` | Run `python -m pipeline.setup_gmail_oauth` once after placing `client_secrets.json`. |
| OAuth browser / consent errors | OAuth client must be type **Desktop**; Gmail API enabled; test user added if app is in Testing mode. |
| `AUTHENTICATIONFAILED` (IMAP) | App Password for `replies` only — not used for send anymore. |
| SMTP timeouts | Expected on blocked ISPs — send uses Gmail API over 443, not SMTP. |
| `Google Drive API has not been used` / 403 | Enable **Drive API** + **Sheets API** on the GCP project; wait a few minutes. |
| `SpreadsheetNotFound` / no access | Share **Pilot CRM** with the service account email as Editor. |
| Apollo `403` / `API_INACCESSIBLE` on `bulk_match` | Create a Master API key (or enable people enrichment scopes). |
| Apollo search OK but no emails | Enrichment scopes missing, or use CSV export → `data/leads.csv`. |
| Rows shifted across columns | Run `python -m pipeline.normalize_sheet` once; `to_sheets` now writes `A{n}:L{n}` only. |
| `send` / followup: nothing to send | Check `Sent` empty, `Verified` is `yes` or `manual`, valid Email. For FU2, Notes must contain `FU1 sent`. |
| Replies: 0 candidates | No matching `In-Reply-To` / `Re: meetings with dental practices…` in the lookback window. |

## 6. Notes

- Openers: after `research --dry-run`, ask Cursor in chat to write factual one-liners into the Signal column.  
- Duplicate emails in column D are skipped on insert.  
- `Лист1` is read for merge during normalize only — never deleted.  
- SMTP code remains commented in `pipeline/email_sender.py` as an emergency fallback.
