#!/usr/bin/env python3
"""Coolify/Hostinger worker for overnight mobile-home form submissions.

The worker reads a queue CSV from a persistent volume, selects the next
unattempted domains, invokes the existing Playwright submission agent, writes
results/screenshots/logs back to the volume, updates state, and optionally loops.
"""

from __future__ import annotations

import csv
import base64
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request, urlopen


APP_ROOT = Path("/app")
AGENT = APP_ROOT / "execution" / "plumbing_website_submission_agent.py"
AGENT_LOG = APP_ROOT / ".tmp" / "plumbing_website_submission_log.json"


def log_event(event: str, **fields: object) -> None:
    payload = {
        "event": event,
        "ts": datetime.now(timezone.utc).isoformat(),
        **fields,
    }
    print(json.dumps(payload, ensure_ascii=True), flush=True)


def env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def normalize_domain(url: str | None) -> str:
    raw = str(url or "").strip()
    if not raw:
        return ""
    if not raw.startswith(("http://", "https://")):
        raw = "https://" + raw
    try:
        return urlparse(raw).netloc.lower().removeprefix("www.")
    except Exception:
        return ""


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def write_csv(path: Path, rows: list[dict[str, object]], fieldnames: list[str] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if fieldnames is None:
        fieldnames = []
        for row in rows:
            for key in row:
                if key not in fieldnames:
                    fieldnames.append(str(key))
    if not fieldnames:
        fieldnames = ["status"]
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def ensure_source_csv(path: Path) -> None:
    """Create the queue CSV from env when a mounted file is not available."""
    if path.exists():
        log_event("queue_exists", path=str(path), bytes=path.stat().st_size)
        return

    path.parent.mkdir(parents=True, exist_ok=True)
    queue_b64 = os.environ.get("QUEUE_CSV_B64", "").strip()
    queue_text = os.environ.get("QUEUE_CSV_TEXT", "").strip()
    queue_url = os.environ.get("QUEUE_CSV_URL", "").strip()

    if queue_b64:
        log_event("queue_from_b64_start", path=str(path), chars=len(queue_b64))
        path.write_bytes(base64.b64decode(queue_b64))
        log_event("queue_from_b64_done", path=str(path), bytes=path.stat().st_size)
        return

    if queue_text:
        log_event("queue_from_text_start", path=str(path), chars=len(queue_text))
        path.write_text(queue_text, encoding="utf-8")
        log_event("queue_from_text_done", path=str(path), bytes=path.stat().st_size)
        return

    if queue_url:
        log_event("queue_download_start", path=str(path), url_host=urlparse(queue_url).netloc)
        request = Request(queue_url, headers={"User-Agent": "website-submission-agent/1.0"})
        with urlopen(request, timeout=60) as response:
            path.write_bytes(response.read())
        log_event("queue_download_done", path=str(path), bytes=path.stat().st_size)
        return

    raise FileNotFoundError(
        f"Source queue not found: {path}. Provide SOURCE_CSV on /data, QUEUE_CSV_B64, QUEUE_CSV_TEXT, or QUEUE_CSV_URL."
    )


def load_state(path: Path) -> dict[str, object]:
    if not path.exists():
        return {"attempted_domains": [], "runs": []}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        backup = path.with_suffix(f".corrupt-{int(time.time())}.json")
        shutil.copy2(path, backup)
        return {"attempted_domains": [], "runs": [], "corrupt_backup": str(backup)}


def save_state(path: Path, state: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.replace(path)


def pick_batch(source_rows: list[dict[str, str]], attempted: set[str], limit: int) -> list[dict[str, str]]:
    picked: list[dict[str, str]] = []
    seen = set(attempted)
    for row in source_rows:
        domain = normalize_domain(row.get("website") or row.get("url"))
        if not domain or domain in seen:
            continue
        seen.add(domain)
        picked.append(row)
        if len(picked) >= limit:
            break
    return picked


def read_agent_results(batch_domains: set[str]) -> list[dict[str, object]]:
    if not AGENT_LOG.exists():
        return []
    try:
        rows = json.loads(AGENT_LOG.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    latest: dict[str, dict[str, object]] = {}
    for row in rows:
        domain = normalize_domain(str(row.get("url") or row.get("website") or row.get("contact_url") or ""))
        if domain and domain in batch_domains:
            latest[domain] = row
    return list(latest.values())


def copy_screenshots(run_dir: Path, result_rows: list[dict[str, object]]) -> None:
    screenshot_dir = run_dir / "screenshots"
    screenshot_dir.mkdir(parents=True, exist_ok=True)
    for row in result_rows:
        for key in ("screenshot_before", "screenshot_after", "review_screenshot"):
            value = str(row.get(key) or "")
            if not value:
                continue
            src = Path(value)
            if src.exists():
                shutil.copy2(src, screenshot_dir / src.name)


def build_command(batch_csv: Path, run_id: str, limit: int) -> list[str]:
    cmd = [
        sys.executable,
        "-X",
        "utf8",
        str(AGENT),
        "--batch",
        str(batch_csv),
        "--limit",
        str(limit),
        "--browser-channel",
        "chromium",
        "--headless",
        "--profile-suffix",
        run_id,
        "--name",
        os.environ.get("SENDER_NAME", ""),
        "--email",
        os.environ.get("SENDER_EMAIL", ""),
        "--phone",
        os.environ.get("SENDER_PHONE", ""),
        "--sender-address",
        os.environ.get("SENDER_ADDRESS", "1455 Clearview Drive"),
        "--sender-city",
        os.environ.get("SENDER_CITY", "McKinney"),
        "--sender-state",
        os.environ.get("SENDER_STATE", "TX"),
        "--sender-postal-code",
        os.environ.get("SENDER_POSTAL_CODE", "75072"),
        "--subject",
        os.environ.get("SUBJECT", "Michigan mobile home case study"),
        "--exact-mobile-home-template",
        "--delay",
        os.environ.get("DELAY_SECONDS", "3"),
    ]
    if env_bool("DRY_RUN"):
        cmd.append("--dry-run")
    if env_bool("REVIEW_BEFORE_SUBMIT"):
        cmd.append("--review-before-submit")
    stop_after = os.environ.get("STOP_AFTER_SUCCESSES", "").strip()
    if stop_after:
        cmd.extend(["--stop-after-successes", stop_after])
    return cmd


def run_once() -> dict[str, object]:
    source_csv = Path(os.environ.get("SOURCE_CSV", "/data/input/mobile-home-strict-form-submission-queue.csv"))
    results_root = Path(os.environ.get("RESULTS_ROOT", "/data/runs"))
    state_file = Path(os.environ.get("STATE_FILE", "/data/state/mobile-home-worker-state.json"))
    limit = int(os.environ.get("BATCH_LIMIT", "25"))
    timeout_seconds = int(os.environ.get("AGENT_TIMEOUT_SECONDS", "1800"))

    log_event("run_once_start", source_csv=str(source_csv), state_file=str(state_file), limit=limit, mode=os.environ.get("RUN_MODE", "once"), dry_run=env_bool("DRY_RUN"))
    ensure_source_csv(source_csv)
    if not os.environ.get("SENDER_NAME") or not os.environ.get("SENDER_EMAIL"):
        raise RuntimeError("SENDER_NAME and SENDER_EMAIL are required")

    state = load_state(state_file)
    attempted = set(str(domain) for domain in state.get("attempted_domains", []))
    source_rows = read_csv(source_csv)
    log_event("queue_loaded", rows=len(source_rows), attempted=len(attempted))
    batch_rows = pick_batch(source_rows, attempted, limit)
    if not batch_rows:
        log_event("batch_empty", rows=len(source_rows), attempted=len(attempted))
        return {"status": "exhausted", "picked": 0}

    run_id = datetime.now(timezone.utc).strftime("mh-%Y%m%d-%H%M%S")
    run_dir = results_root / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    batch_csv = run_dir / "input.csv"
    write_csv(batch_csv, batch_rows)
    batch_domains = {
        normalize_domain(row.get("website") or row.get("url"))
        for row in batch_rows
        if normalize_domain(row.get("website") or row.get("url"))
    }
    log_event("batch_picked", run_id=run_id, picked=len(batch_rows), domains=sorted(batch_domains)[:5])

    stdout_path = run_dir / "stdout.log"
    stderr_path = run_dir / "stderr.log"
    started = time.time()
    cmd = build_command(batch_csv, run_id, limit)
    log_event("agent_start", run_id=run_id, timeout_seconds=timeout_seconds, batch_csv=str(batch_csv))
    with stdout_path.open("w", encoding="utf-8") as stdout, stderr_path.open("w", encoding="utf-8") as stderr:
        try:
            proc = subprocess.run(cmd, cwd=APP_ROOT, stdout=stdout, stderr=stderr, text=True, timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            log_event("agent_timeout", run_id=run_id, timeout_seconds=timeout_seconds)
            raise

    result_rows = read_agent_results(batch_domains)
    log_event("agent_done", run_id=run_id, returncode=proc.returncode, result_rows=len(result_rows))
    write_csv(run_dir / "results.csv", result_rows)
    copy_screenshots(run_dir, result_rows)
    if AGENT_LOG.exists():
        shutil.copy2(AGENT_LOG, run_dir / "plumbing_website_submission_log.json")

    attempted.update(batch_domains)
    state["attempted_domains"] = sorted(attempted)
    runs = list(state.get("runs", []))
    runs.append(
        {
            "run_id": run_id,
            "started_at": datetime.fromtimestamp(started, timezone.utc).isoformat(),
            "duration_seconds": round(time.time() - started, 1),
            "returncode": proc.returncode,
            "input_count": len(batch_rows),
            "logged_count": len(result_rows),
            "run_dir": str(run_dir),
        }
    )
    state["runs"] = runs
    save_state(state_file, state)
    return runs[-1]


def main() -> int:
    mode = os.environ.get("RUN_MODE", "once").strip().lower()
    sleep_seconds = int(os.environ.get("LOOP_SLEEP_SECONDS", "300"))
    while True:
        try:
            result = run_once()
            print(json.dumps(result, indent=2), flush=True)
            if result.get("status") == "exhausted":
                return 0
        except Exception as exc:
            print(json.dumps({"status": "error", "error": str(exc)}, indent=2), flush=True)
            if mode != "loop":
                return 1
        if mode != "loop":
            return 0
        time.sleep(sleep_seconds)


if __name__ == "__main__":
    raise SystemExit(main())

