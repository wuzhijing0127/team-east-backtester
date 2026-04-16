# Prosperity Uploader

Local experiment agent for the IMC Prosperity platform. Upload strategy files, track submissions, download result artifacts, and compare metrics across runs.

## Setup

```bash
cd prosperity-uploader
pip install -r requirements.txt
```

## Authentication

Get your bearer token from the IMC Prosperity website:

1. Log in to the platform in your browser
2. Open DevTools (F12) > Network tab
3. Find any API request to `3dzqiahkw1.execute-api.eu-west-1.amazonaws.com`
4. Copy the `Authorization: Bearer <token>` value

Set it via environment variable (recommended):

```bash
export PROSPERITY_TOKEN="your-jwt-token-here"
```

Or pass it on each command:

```bash
python main.py --token "your-jwt-token-here" upload path/to/algo.py
```

## Usage

### Upload a single strategy

```bash
python main.py upload ../strategies/parameterized_v5.py
```

### Batch upload all strategies in a directory

```bash
python main.py batch ../teameastbt/resources/round1/variants/
```

Dedup flags:

```bash
# Re-upload even if the same file hash was uploaded before
python main.py batch ../strategies/ --force-upload

# Resume previous run for matching hash instead of re-uploading
python main.py batch ../strategies/ --reuse-latest-run
```

### Poll a submission until it completes

```bash
python main.py poll 179369
```

Polling watches for `status == "FINISHED"` (configurable in config.yaml).

### Get the graph/artifact URL and download it

```bash
python main.py graph 179369 --download --output artifact_179369.json
```

### Analyze a locally saved artifact

```bash
# Inspect the schema (useful for unknown artifacts)
python main.py analyze runs/2026-04-15_23-00-00_v4o_L1_14/artifact.json --inspect

# Compute metrics
python main.py analyze runs/2026-04-15_23-00-00_v4o_L1_14/artifact.json --save
```

### List recent submissions

```bash
python main.py list --page 1 --page-size 20
```

### Inspect a submissions response for field mappings

If you saved a raw submissions list response and want to verify field names:

```bash
python main.py inspect-submissions runs/some_run/submissions_list_response.json
```

This prints detected field paths and suggests config mappings.

### View the leaderboard

```bash
python main.py leaderboard --limit 20

# Include artifact and run directory paths
python main.py leaderboard --limit 20 -v
```

### Resume interrupted workflows

If a batch run was interrupted (e.g. network failure after upload but before artifact download):

```bash
python main.py resume
```

This scans `runs/` for incomplete directories and picks up where it left off. It handles all interruption points: upload done but no submission found, submission found but not polled, graph fetched but artifact not downloaded, artifact downloaded but metrics not computed.

## Confirmed API schema

### Submissions list

The submissions list endpoint returns:

```json
{
  "success": true,
  "status": 200,
  "data": {
    "items": [
      {
        "id": 179369,
        "status": "FINISHED",
        "filename": "v4o_L1_14.py",
        "submittedAt": "2026-04-15T10:00:00Z",
        "teamId": ...,
        "roundId": ...,
        "submittedBy": {"firstName": ..., "lastName": ...},
        "active": true,
        "simulationApplicationAlgoSubmissionIdentifier": ...
      }
    ],
    "page": 1,
    "pageSize": 50,
    "total": 42
  }
}
```

### Submission matching

After upload, the tool matches by:
1. Exact `filename` match
2. `submittedAt >= (upload_time - 120s tolerance)` (configurable)
3. Newest match wins

### Graph endpoint

Returns `{"data": {"url": "https://...signed-s3-url..."}}`. The signed URL is downloaded immediately since it expires.

## Configuration

Edit `config.yaml` to adjust endpoints, timing, and field mappings.

| Setting | Default | Description |
|---------|---------|-------------|
| `poll_interval_seconds` | 10 | How often to check submission status |
| `upload_interval_seconds` | 15 | Delay between batch uploads |
| `max_retries` | 5 | Max HTTP retries before failing |
| `concurrency` | 1 | Sequential uploads (safe default) |
| `status_finished` | `FINISHED` | Status value indicating completion |
| `submission_match_time_tolerance_seconds` | 120 | Time window for matching uploads to submissions |

## What's stored

Each run creates a directory under `runs/`:

```
runs/
  2026-04-15_23-00-00_v4o_L1_14/
    upload_response.json              # Raw upload API response
    submissions_list_response.json    # Raw list response used for matching
    submission.json                   # Selected submission metadata
    graph_response.json               # Graph endpoint response
    artifact.json                     # Full raw artifact (downloaded immediately)
    summary.json                      # Computed metrics
```

Aggregate results go to:
- `results/summary.csv` — CSV leaderboard with all metrics
- `results/results.sqlite` — SQLite database with full history

## What remains to manually inspect

The **graph endpoint path** is the only remaining placeholder. To confirm it:

1. Open a completed submission in your browser
2. Open DevTools > Network tab
3. Look for the GET request that returns `{"data": {"url": "https://...s3..."}}`
4. Note the URL path pattern
5. Update `graph_endpoint` in `config.yaml` using `{submission_id}` as the ID placeholder

Everything else (upload, submissions list, polling, artifact download, metrics) is fully wired.

## Debug mode

```bash
python main.py --debug batch ../strategies/
```

This enables verbose logging including full request/response details. In debug mode, each polling response is also saved to the run directory.
