#!/usr/bin/env python3
"""CLI entry point for the Prosperity experiment agent."""

from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# Add project dir to path for sibling imports
sys.path.insert(0, str(Path(__file__).parent))

from artifact_parser import inspect_schema, parse_artifact
from auth import TokenProvider
from client import APIClient
from config import Config
from exceptions import ProsperityUploaderError
from graph import download_artifact, get_graph_url
from metrics import summarize_artifact
from models import RunRecord, SummaryMetrics
from storage import (
    Database,
    append_to_csv,
    artifact_path,
    create_run_dir,
    load_csv_leaderboard,
    save_error,
    save_graph_result,
    save_submission,
    save_summary,
    save_upload_result,
)
from submissions import (
    find_submission_for_upload,
    inspect_submissions_response,
    list_submissions,
    poll_submission_until_ready,
)
from uploader import upload_algo
from utils import file_sha256, load_json, save_json, strategy_name_from_path

logger = logging.getLogger("prosperity")


# ── Full workflow for one file ────────────────────────────────────

def run_single(
    client: APIClient,
    config: Config,
    db: Database,
    file_path: str,
    force_upload: bool = False,
    reuse_latest_run: bool = False,
) -> Optional[SummaryMetrics]:
    """Upload a file, poll for completion, download artifact, compute metrics.

    Args:
        force_upload: Ignore hash dedup, always upload.
        reuse_latest_run: If hash matches a previous upload, resume that run
                          instead of uploading again.

    Returns SummaryMetrics on success, None on failure.
    """
    strategy_name = strategy_name_from_path(file_path)
    file_hash = file_sha256(file_path)

    # Dedup / reuse logic
    if not force_upload:
        prev = db.find_upload_by_hash(file_hash)
        if prev:
            if reuse_latest_run:
                logger.info(
                    "File %s (hash %s) was previously uploaded as submission %s. "
                    "Reusing that run.",
                    file_path,
                    file_hash[:12],
                    prev.get("submission_id"),
                )
                # Try to resume from the previous submission
                sub_id = prev.get("submission_id")
                if sub_id:
                    return _resume_from_submission(
                        client, config, db, strategy_name, sub_id, file_path
                    )
                logger.warning("Previous upload had no submission ID, falling through to re-upload")
            else:
                logger.warning(
                    "File %s (hash %s) was already uploaded. Skipping. "
                    "Use --force-upload to re-upload, or --reuse-latest-run to resume.",
                    file_path,
                    file_hash[:12],
                )
                return None

    run_dir = create_run_dir(config, strategy_name)
    logger.info("=== Processing %s ===", strategy_name)

    try:
        # 1. Upload
        logger.info("Step 1: Uploading %s", file_path)
        upload_result = upload_algo(client, config, file_path)
        save_upload_result(run_dir, upload_result)
        db.insert_upload(upload_result, strategy_name)

        # 2. Find submission
        logger.info("Step 2: Finding submission")
        submission = find_submission_for_upload(
            client,
            config,
            filename=upload_result.filename,
            upload_time=upload_result.timestamp,
            submission_id_hint=upload_result.submission_id,
            run_dir=run_dir,
        )
        save_submission(run_dir, submission)
        db.upsert_submission(submission)

        # 3. Poll until ready
        logger.info("Step 3: Polling until ready (ID: %s)", submission.submission_id)
        final_sub = poll_submission_until_ready(
            client, config, submission.submission_id, run_dir=run_dir
        )
        save_submission(run_dir, final_sub)
        db.upsert_submission(final_sub)

        # 4. Get graph / artifact URL
        logger.info("Step 4: Fetching graph URL")
        graph_result = get_graph_url(client, config, final_sub.submission_id)
        save_graph_result(run_dir, graph_result)

        # 5. Download artifact immediately (signed URL is temporary)
        logger.info("Step 5: Downloading artifact")
        art_path = str(artifact_path(run_dir))
        artifact_data = download_artifact(client, graph_result.artifact_url, art_path)
        db.insert_artifact(
            final_sub.submission_id,
            graph_result.artifact_url,
            art_path,
            graph_result.raw_response,
        )

        # 6. Parse and compute metrics
        logger.info("Step 6: Computing metrics")
        parsed = parse_artifact(artifact_data)
        summary = summarize_artifact(parsed, strategy_name, final_sub.submission_id)
        # Attach metadata to summary
        summary.filename = final_sub.filename or upload_result.filename
        summary.submitted_at = final_sub.submitted_at.isoformat() if final_sub.submitted_at else None
        summary.artifact_path = art_path
        summary.run_dir = str(run_dir)

        save_summary(run_dir, summary)
        append_to_csv(config, summary)
        db.insert_summary(summary)

        logger.info(
            "=== %s complete: PnL=%.2f, MaxDD=%.2f ===",
            strategy_name,
            summary.final_pnl,
            summary.max_drawdown,
        )
        return summary

    except ProsperityUploaderError as e:
        logger.error("Failed processing %s: %s", strategy_name, e)
        save_error(run_dir, str(e))
        return None
    except Exception as e:
        logger.error("Unexpected error processing %s: %s", strategy_name, e, exc_info=True)
        save_error(run_dir, str(e))
        return None


def _resume_from_submission(
    client: APIClient,
    config: Config,
    db: Database,
    strategy_name: str,
    submission_id: str,
    file_path: str,
) -> Optional[SummaryMetrics]:
    """Resume workflow from a known submission ID (skip upload)."""
    run_dir = create_run_dir(config, strategy_name)

    try:
        # Poll until ready
        final_sub = poll_submission_until_ready(client, config, submission_id, run_dir=run_dir)
        save_submission(run_dir, final_sub)
        db.upsert_submission(final_sub)

        # Get graph
        graph_result = get_graph_url(client, config, final_sub.submission_id)
        save_graph_result(run_dir, graph_result)

        # Download artifact
        art_path = str(artifact_path(run_dir))
        artifact_data = download_artifact(client, graph_result.artifact_url, art_path)
        db.insert_artifact(
            final_sub.submission_id,
            graph_result.artifact_url,
            art_path,
            graph_result.raw_response,
        )

        # Compute metrics
        parsed = parse_artifact(artifact_data)
        summary = summarize_artifact(parsed, strategy_name, final_sub.submission_id)
        summary.filename = final_sub.filename or Path(file_path).name
        summary.submitted_at = final_sub.submitted_at.isoformat() if final_sub.submitted_at else None
        summary.artifact_path = art_path
        summary.run_dir = str(run_dir)

        save_summary(run_dir, summary)
        append_to_csv(config, summary)
        db.insert_summary(summary)
        return summary

    except Exception as e:
        logger.error("Failed resuming %s: %s", strategy_name, e)
        save_error(run_dir, str(e))
        return None


# ── Batch runner ──────────────────────────────────────────────────

def run_batch(
    client: APIClient,
    config: Config,
    db: Database,
    directory: str,
    force_upload: bool = False,
    reuse_latest_run: bool = False,
) -> list[SummaryMetrics]:
    """Process all .py files in a directory sequentially."""
    dir_path = Path(directory)
    if not dir_path.is_dir():
        logger.error("Not a directory: %s", directory)
        return []

    py_files = sorted(dir_path.glob("*.py"))
    if not py_files:
        logger.warning("No .py files found in %s", directory)
        return []

    logger.info("Batch mode: %d strategy files in %s", len(py_files), directory)
    results: list[SummaryMetrics] = []

    for i, py_file in enumerate(py_files, 1):
        logger.info("--- Batch %d/%d: %s ---", i, len(py_files), py_file.name)

        summary = run_single(
            client, config, db, str(py_file),
            force_upload=force_upload,
            reuse_latest_run=reuse_latest_run,
        )
        if summary:
            results.append(summary)

        # Conservative sleep between uploads
        if i < len(py_files):
            logger.info(
                "Waiting %.0fs before next upload...",
                config.upload_interval_seconds,
            )
            time.sleep(config.upload_interval_seconds)

    # Print leaderboard
    if results:
        _print_batch_leaderboard(results)

    return results


def _print_batch_leaderboard(results: list[SummaryMetrics]) -> None:
    results_sorted = sorted(results, key=lambda s: s.final_pnl, reverse=True)
    print("\n" + "=" * 80)
    print("BATCH RESULTS LEADERBOARD")
    print("=" * 80)
    print(
        f"{'Rank':<6}{'Strategy':<25}{'SubID':>8}"
        f"{'PnL':>12}{'MaxPnL':>10}{'MinPnL':>10}{'MaxDD':>10}{'Pts':>7}"
    )
    print("-" * 80)
    for rank, s in enumerate(results_sorted, 1):
        print(
            f"{rank:<6}{s.strategy_name:<25}{s.submission_id:>8}"
            f"{s.final_pnl:>12,.2f}{s.max_pnl:>10,.2f}{s.min_pnl:>10,.2f}"
            f"{s.max_drawdown:>10,.2f}{s.num_points:>7}"
        )
    print("=" * 80)


# ── CLI commands ──────────────────────────────────────────────────

def cmd_upload(args: argparse.Namespace, client: APIClient, config: Config, db: Database) -> None:
    upload_result = upload_algo(client, config, args.file)
    save_json(upload_result.to_dict(), Path("upload_response_debug.json"))
    print(f"Upload successful: HTTP {upload_result.status_code}")
    print(f"Submission ID: {upload_result.submission_id or '(not in response)'}")
    print(f"Response: {upload_result.response_body}")


def cmd_batch(args: argparse.Namespace, client: APIClient, config: Config, db: Database) -> None:
    run_batch(
        client, config, db, args.directory,
        force_upload=args.force_upload,
        reuse_latest_run=args.reuse_latest_run,
    )


def cmd_poll(args: argparse.Namespace, client: APIClient, config: Config, db: Database) -> None:
    sub = poll_submission_until_ready(client, config, args.submission_id)
    print(f"Submission {sub.submission_id}: {sub.status.value}")
    print(f"Filename: {sub.filename}")
    print(f"Submitted at: {sub.submitted_at}")
    print(f"Raw: {sub.raw}")


def cmd_graph(args: argparse.Namespace, client: APIClient, config: Config, db: Database) -> None:
    result = get_graph_url(client, config, args.submission_id)
    print(f"Artifact URL: {result.artifact_url}")

    if args.download:
        out = args.output or f"artifact_{args.submission_id}.json"
        data = download_artifact(client, result.artifact_url, out)
        print(f"Downloaded to {out} ({len(data) if isinstance(data, (list, dict)) else '?'} entries)")


def cmd_analyze(args: argparse.Namespace, client: APIClient, config: Config, db: Database) -> None:
    data = load_json(args.artifact_path)

    if args.inspect:
        print("Schema inspection:")
        print(inspect_schema(data))
        return

    parsed = parse_artifact(data)
    name = strategy_name_from_path(args.artifact_path)
    summary = summarize_artifact(parsed, name, args.submission_id or "local")

    print(f"\nMetrics for {name}:")
    for key, val in summary.to_dict().items():
        if key not in ("raw_fields_found",):
            print(f"  {key}: {val}")

    if args.save:
        append_to_csv(config, summary)
        print(f"\nSaved to {config.results_csv}")


def cmd_list(args: argparse.Namespace, client: APIClient, config: Config, db: Database) -> None:
    subs, raw = list_submissions(client, config, page=args.page, page_size=args.page_size)
    if not subs:
        print("No submissions found.")
        return

    print(f"{'ID':<12}{'Status':<12}{'Filename':<30}{'Submitted At'}")
    print("-" * 75)
    for s in subs:
        ts_str = s.submitted_at.strftime("%Y-%m-%d %H:%M:%S") if s.submitted_at else "?"
        print(f"{s.submission_id:<12}{s.status.value:<12}{(s.filename or '?'):<30}{ts_str}")


def cmd_inspect_submissions(args: argparse.Namespace, client: APIClient, config: Config, db: Database) -> None:
    """Inspect a saved submissions list response to discover field mappings."""
    data = load_json(args.response_path)
    print(inspect_submissions_response(data))


def cmd_leaderboard(args: argparse.Namespace, client: APIClient, config: Config, db: Database) -> None:
    rows = db.get_leaderboard(limit=args.limit)
    if not rows:
        print("No results yet. Run some strategies first.")
        return

    print(
        f"\n{'Rank':<5}{'Strategy':<22}{'SubID':>8}{'Filename':<20}"
        f"{'PnL':>11}{'MaxPnL':>10}{'MinPnL':>10}{'MaxDD':>10}{'Pts':>6}"
    )
    print("-" * 102)
    for rank, r in enumerate(rows, 1):
        fname = (r.get("filename") or "?")[:18]
        print(
            f"{rank:<5}"
            f"{r['strategy_name']:<22}"
            f"{r['submission_id'] or '?':>8}"
            f"{fname:<20}"
            f"{r['final_pnl'] or 0:>11,.2f}"
            f"{r['max_pnl'] or 0:>10,.2f}"
            f"{r['min_pnl'] or 0:>10,.2f}"
            f"{r['max_drawdown'] or 0:>10,.2f}"
            f"{r['num_points'] or 0:>6}"
        )

    if args.verbose:
        print("\nDetailed paths:")
        for rank, r in enumerate(rows, 1):
            print(f"  #{rank} artifact: {r.get('artifact_path', '?')}")
            print(f"       run_dir:  {r.get('run_dir', '?')}")


def cmd_resume(args: argparse.Namespace, client: APIClient, config: Config, db: Database) -> None:
    """Resume interrupted workflows by scanning run directories."""
    runs_dir = Path(config.output_dir)
    if not runs_dir.exists():
        print("No runs directory found.")
        return

    resumed = 0
    skipped = 0
    for run_dir in sorted(runs_dir.iterdir()):
        if not run_dir.is_dir():
            continue

        has_upload = (run_dir / "upload_response.json").exists()
        has_artifact = (run_dir / "artifact.json").exists()
        has_summary = (run_dir / "summary.json").exists()
        has_graph = (run_dir / "graph_response.json").exists()
        has_submission = (run_dir / "submission.json").exists()

        if has_summary:
            skipped += 1
            continue  # Already complete

        # Extract strategy name from directory (format: YYYY-MM-DD_HH-MM-SS_name)
        parts = run_dir.name.split("_", 3)
        strategy_name = parts[3] if len(parts) > 3 else run_dir.name

        logger.info("Resuming incomplete run: %s", run_dir.name)

        try:
            # Case 1: have artifact but no summary — just re-analyze
            if has_artifact and not has_summary:
                art_data = load_json(artifact_path(run_dir))
                sub_id = "unknown"
                if has_submission:
                    sub_data = load_json(run_dir / "submission.json")
                    sub_id = sub_data.get("submission_id", "unknown")
                parsed = parse_artifact(art_data)
                summary = summarize_artifact(parsed, strategy_name, sub_id)
                summary.artifact_path = str(artifact_path(run_dir))
                summary.run_dir = str(run_dir)
                save_summary(run_dir, summary)
                append_to_csv(config, summary)
                db.insert_summary(summary)
                resumed += 1
                continue

            # Case 2: have graph response but no artifact — download and analyze
            if has_graph and not has_artifact:
                graph_data = load_json(run_dir / "graph_response.json")
                url = graph_data.get("artifact_url", "")
                sub_id = graph_data.get("submission_id", "unknown")
                if url:
                    art_data = download_artifact(client, url, str(artifact_path(run_dir)))
                    db.insert_artifact(sub_id, url, str(artifact_path(run_dir)), graph_data)
                    parsed = parse_artifact(art_data)
                    summary = summarize_artifact(parsed, strategy_name, sub_id)
                    summary.artifact_path = str(artifact_path(run_dir))
                    summary.run_dir = str(run_dir)
                    save_summary(run_dir, summary)
                    append_to_csv(config, summary)
                    db.insert_summary(summary)
                    resumed += 1
                    continue

            # Case 3: have submission but no graph — poll, fetch graph, download, analyze
            if has_submission and not has_graph:
                sub_data = load_json(run_dir / "submission.json")
                sub_id = sub_data.get("submission_id", "")
                if not sub_id:
                    continue

                # Poll if not finished
                status = sub_data.get("status", "")
                if status not in ("FINISHED", "completed"):
                    final_sub = poll_submission_until_ready(client, config, sub_id, run_dir=run_dir)
                    save_submission(run_dir, final_sub)
                    db.upsert_submission(final_sub)
                    sub_id = final_sub.submission_id

                graph_result = get_graph_url(client, config, sub_id)
                save_graph_result(run_dir, graph_result)
                art_data = download_artifact(
                    client, graph_result.artifact_url, str(artifact_path(run_dir))
                )
                db.insert_artifact(sub_id, graph_result.artifact_url, str(artifact_path(run_dir)), graph_result.raw_response)
                parsed = parse_artifact(art_data)
                summary = summarize_artifact(parsed, strategy_name, sub_id)
                summary.artifact_path = str(artifact_path(run_dir))
                summary.run_dir = str(run_dir)
                save_summary(run_dir, summary)
                append_to_csv(config, summary)
                db.insert_summary(summary)
                resumed += 1
                continue

            # Case 4: have upload but no submission — try to find submission
            if has_upload and not has_submission:
                upload_data = load_json(run_dir / "upload_response.json")
                filename = upload_data.get("filename", "")
                upload_time_str = upload_data.get("timestamp", "")
                sub_id_hint = upload_data.get("submission_id")

                upload_time = datetime.fromisoformat(upload_time_str) if upload_time_str else datetime.now(timezone.utc)

                submission = find_submission_for_upload(
                    client, config,
                    filename=filename,
                    upload_time=upload_time,
                    submission_id_hint=sub_id_hint,
                    run_dir=run_dir,
                )
                save_submission(run_dir, submission)
                db.upsert_submission(submission)

                # Now continue with poll → graph → download → analyze
                final_sub = poll_submission_until_ready(client, config, submission.submission_id, run_dir=run_dir)
                save_submission(run_dir, final_sub)
                db.upsert_submission(final_sub)

                graph_result = get_graph_url(client, config, final_sub.submission_id)
                save_graph_result(run_dir, graph_result)
                art_data = download_artifact(
                    client, graph_result.artifact_url, str(artifact_path(run_dir))
                )
                db.insert_artifact(final_sub.submission_id, graph_result.artifact_url, str(artifact_path(run_dir)), graph_result.raw_response)
                parsed = parse_artifact(art_data)
                summary = summarize_artifact(parsed, strategy_name, final_sub.submission_id)
                summary.artifact_path = str(artifact_path(run_dir))
                summary.run_dir = str(run_dir)
                save_summary(run_dir, summary)
                append_to_csv(config, summary)
                db.insert_summary(summary)
                resumed += 1

        except Exception as e:
            logger.error("Failed to resume %s: %s", run_dir.name, e)
            save_error(run_dir, str(e))

    print(f"Resumed {resumed} incomplete runs. ({skipped} already complete)")


# ── Main ──────────────────────────────────────────────────────────

def setup_logging(debug: bool = False) -> None:
    level = logging.DEBUG if debug else logging.INFO
    fmt = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
    logging.basicConfig(level=level, format=fmt, datefmt="%H:%M:%S")
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("requests").setLevel(logging.WARNING)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="prosperity-uploader",
        description="IMC Prosperity experiment agent — upload, track, and compare strategies.",
    )
    parser.add_argument("--config", type=str, help="Path to config YAML file")
    parser.add_argument("--token", type=str, help="Bearer token (overrides env/config)")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")

    sub = parser.add_subparsers(dest="command", required=True)

    # upload
    p_upload = sub.add_parser("upload", help="Upload a single strategy file")
    p_upload.add_argument("file", type=str, help="Path to .py strategy file")

    # batch
    p_batch = sub.add_parser("batch", help="Batch upload all .py files in a directory")
    p_batch.add_argument("directory", type=str, help="Directory containing .py files")
    p_batch.add_argument("--force-upload", action="store_true", help="Ignore hash dedup, always re-upload")
    p_batch.add_argument("--reuse-latest-run", action="store_true", help="Resume previous run for matching hash instead of re-uploading")

    # poll
    p_poll = sub.add_parser("poll", help="Poll a submission until it completes")
    p_poll.add_argument("submission_id", type=str)

    # graph
    p_graph = sub.add_parser("graph", help="Get graph/artifact URL for a submission")
    p_graph.add_argument("submission_id", type=str)
    p_graph.add_argument("--download", action="store_true", help="Also download the artifact")
    p_graph.add_argument("--output", type=str, help="Output path for downloaded artifact")

    # analyze
    p_analyze = sub.add_parser("analyze", help="Analyze a locally saved artifact JSON")
    p_analyze.add_argument("artifact_path", type=str)
    p_analyze.add_argument("--submission-id", type=str, help="Submission ID for labeling")
    p_analyze.add_argument("--inspect", action="store_true", help="Print schema inspection only")
    p_analyze.add_argument("--save", action="store_true", help="Save metrics to CSV")

    # list
    p_list = sub.add_parser("list", help="List recent submissions")
    p_list.add_argument("--page", type=int, default=1)
    p_list.add_argument("--page-size", type=int, default=50)

    # inspect-submissions
    p_inspect = sub.add_parser(
        "inspect-submissions",
        help="Inspect a saved submissions list response JSON for field mappings",
    )
    p_inspect.add_argument("response_path", type=str, help="Path to saved submissions JSON response")

    # leaderboard
    p_lb = sub.add_parser("leaderboard", help="Show stored results leaderboard")
    p_lb.add_argument("--limit", type=int, default=50)
    p_lb.add_argument("--verbose", "-v", action="store_true", help="Show artifact/run paths")

    # resume
    sub.add_parser("resume", help="Resume interrupted workflows")

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    setup_logging(debug=args.debug)

    # Load config
    cli_overrides = {}
    if args.token:
        cli_overrides["bearer_token"] = args.token
    config = Config.load(config_path=args.config, cli_overrides=cli_overrides)

    # Commands that don't need auth
    if args.command in ("analyze", "leaderboard", "inspect-submissions"):
        token_provider = TokenProvider(config.bearer_token or "")
        client = APIClient(config, token_provider)
        db = Database(config)
        try:
            {
                "analyze": cmd_analyze,
                "leaderboard": cmd_leaderboard,
                "inspect-submissions": cmd_inspect_submissions,
            }[args.command](args, client, config, db)
        finally:
            db.close()
        return

    # All other commands need a valid token
    config.validate()
    token_provider = TokenProvider(config.bearer_token)
    client = APIClient(config, token_provider)
    db = Database(config)

    commands = {
        "upload": cmd_upload,
        "batch": cmd_batch,
        "poll": cmd_poll,
        "graph": cmd_graph,
        "list": cmd_list,
        "resume": cmd_resume,
    }

    try:
        commands[args.command](args, client, config, db)
    except ProsperityUploaderError as e:
        logger.error("Error: %s", e)
        sys.exit(1)
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
        sys.exit(130)
    finally:
        db.close()


if __name__ == "__main__":
    main()
