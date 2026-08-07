# TrustCSI Email → Splunk Lookup Sync

Reads TrustCSI case emails from Zimbra, parses case fields, and updates Splunk CSV lookups (`G#####_Ticket_Status.csv`).

**Closed cases** set:

| Column | Value |
|--------|--------|
| Status | `Resolved` |
| Remark | resolution text (True/False Positive labels removed) |
| Matrix | `False Positive` by default; `True Positive` only when resolution contains that label |

**Non-closed TrustCSI replies** set `Actionable` to `Yes` (To includes IT Support) or `No`.

CSV column order on write:

`TicketNumber, Severity, Status, Remark, Matrix, Actionable`

## Setup

```bash
python3 -m pip install -r requirements.txt
cp config.json.example config.json
cp .env.example .env
```

Edit `config.json` (non-secrets) and `.env` (credentials):

```env
ZIMBRA_EMAIL=you@example.com
ZIMBRA_PASSWORD=your-password
SPLUNK_USERNAME=your-splunk-username
SPLUNK_PASSWORD=your-splunk-password
```

| Config key | Purpose |
|------------|---------|
| `host` | Zimbra mail host |
| `folder_path` | Folder name/path or folder id (e.g. `Inbox` or `373`) |
| `limit` | Max newest messages to scan each run |
| `stop_at_known` | If `true`, stop scanning when a previously saved message id is hit |
| `closed_mode` | `overwrite` (default) reprocess closed cases; `skip` drops duplicate closed cases (same case number), including cases already saved in `emails.json`. Does **not** filter by message id new/old |
| `actionable_mode` | `overwrite` (default) always write Actionable; `skip` leaves Splunk rows that already have an Actionable value |
| `splunk_web_url` / `splunk_rest_url` | Splunk UI or REST base URL (REST defaults to host `:8089` if empty) |
| `splunk_app` / `splunk_owner` | App/owner namespace for lookup writes |
| `splunk_verify_tls` | TLS verify for Splunk REST |
| `splunk_timeout` | Search job timeout (seconds) |

Lookup name is derived from the case number: first 5 digits → `G50095_Ticket_Status.csv`.

## Usage

```bash
python3 main.py --method <method> [options]
```

| Method | What it does |
|--------|----------------|
| `soap` | Login test (default) |
| `list` | Print newest emails in the folder |
| `find` | Run the built-in cust/G50095 search helper |
| `watch` | Scan for new **Closed** emails and save them to `output/emails.json` (no Splunk) |
| `sync` | Scan Closed + Actionable emails, save Closed to JSON, update Splunk |
| `update-splunk` | Scan Closed emails and update Splunk only (no JSON save) |

Common options:

```bash
python3 main.py --method sync
python3 main.py --method sync --folder-path Inbox --limit 50
python3 main.py --method sync --output output
python3 main.py --method list --folder-path 373
python3 main.py --self-test
python3 main.py --config /path/to/config.json --method soap
```

| Flag | Description |
|------|-------------|
| `--folder-path` | Override `folder_path` from config |
| `--limit` | Override `limit` from config |
| `--output` | Output dir for `sync` / `watch` (default: `output`) |
| `--config` | Path to config file (default: `config.json`) |
| `--self-test` | Run local parser/SPL checks and exit |

## Typical workflow

1. Confirm Zimbra login:

   ```bash
   python3 main.py --method soap
   ```

2. Inspect the folder:

   ```bash
   python3 main.py --method list
   ```

3. Sync and update Splunk (main path):

   ```bash
   python3 main.py --method sync
   ```

Closed rows land in `output/emails.json`. Splunk updates use the matching `G#####_Ticket_Status.csv` lookup.

## Tests

```bash
python3 main.py --self-test
python3 -m unittest test_watch.py
```
