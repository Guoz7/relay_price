"""Maintain a rolling ledger of station health and price samples."""
from __future__ import annotations

import argparse
import gzip
import io
import json
import math
import re
import sys
import tempfile
import zlib
from bisect import bisect_left
from datetime import datetime, timedelta, timezone
from operator import itemgetter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pricing
from consumer import market_index, model_medians

TRACKED: list[str] = [
    "claude-opus-5-5", "claude-sonnet-5-5", "gpt-6-astra",
    "gemini-3.8-flash", "deepseek-v4.1-flash",
]
LABELS: dict[str, str] = dict(zip(TRACKED, [
    "Claude Opus 5.5", "Claude Sonnet 5.5", "GPT-6 Astra",
    "Gemini 3.8 Flash", "DeepSeek V4.1 Flash",
]))
LEDGER_NAME = "timeseries.json.gz"
MAX_POINTS = 120
WINDOW_DAYS = 30
LANES = {"standard", "promo", "welfare"}
_HISTORY_NAME = re.compile(r"[0-9]{8}T[0-9]{6}\.json\.gz")
_TS = itemgetter(0)


def parse_history_key(key: str) -> tuple[str, str, str]:
    dom, rest = key.split("|", 1)
    head, sep, last = rest.rpartition("|")
    if sep and last in LANES:
        return dom, head, last
    return dom, rest, "standard"


def _utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _iso_utc(iso: str) -> datetime | None:
    if isinstance(iso, str):
        try:
            return _utc(datetime.fromisoformat(iso))
        except (ValueError, OverflowError):
            pass
    return None


def _hour(dt: datetime) -> str:
    return dt.isoformat()[:13] + ":00Z"


def hour_ts(iso: str) -> str | None:
    dt = _iso_utc(iso)
    return _hour(dt) if dt is not None else None


def empty_ledger() -> dict:
    return {"v": 1, "models": TRACKED.copy(), "labels": LABELS.copy(), "market": [], "s": {}}


def _read_json(path: Path, *, compressed: bool = False) -> dict | None:
    opener = gzip.open if compressed else open
    try:
        with opener(path, "rt", encoding="utf-8") as fh:
            value = json.load(fh)
    except (OSError, EOFError, ValueError, RecursionError, zlib.error):
        return None
    return value if isinstance(value, dict) else None


def _finite(value: object) -> bool:
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _price(value: object) -> float | None:
    if _finite(value) and value > 0:
        rounded = float(format(value, ".4g"))
        if math.isfinite(rounded):
            return rounded
    return None


def _domain(value: object) -> bool:
    return isinstance(value, str) and bool(value) and not re.search(r"[\ud800-\udfff]", value)


def _valid_rows(rows: object, *, station: bool) -> bool:
    if not isinstance(rows, list):
        return False
    previous = ""
    for row in rows:
        width = 5 if station else len(TRACKED) + 1
        if not isinstance(row, list) or len(row) != width:
            return False
        ts = row[0]
        if not isinstance(ts, str) or ts <= previous or hour_ts(ts) != ts:
            return False
        previous = ts
        if station:
            _, status, elapsed_ms, index, prices = row
            if status is not None and (type(status) is not int or status not in (0, 1)):
                return False
            if elapsed_ms is not None and type(elapsed_ms) is not int:
                return False
            if index is not None and not _finite(index):
                return False
        else:
            prices = row[1:]
        if not isinstance(prices, list) or len(prices) != len(TRACKED):
            return False
        if any(p is not None and (not _finite(p) or p <= 0) for p in prices):
            return False
    return True


def load_ledger(path: Path) -> dict:
    ledger = _read_json(path, compressed=True)
    empty = empty_ledger()
    if ledger is None or type(ledger.get("v")) is not int:
        return empty
    if any(ledger.get(key) != empty[key] for key in ("v", "models", "labels")):
        return empty
    stations = ledger.get("s")
    if not isinstance(stations, dict) or not _valid_rows(ledger.get("market"), station=False):
        return empty
    if any(not _domain(dom) or not _valid_rows(rows, station=True) for dom, rows in stations.items()):
        return empty
    return {key: ledger[key] for key in empty}


def _ledger_bytes(ledger: dict) -> bytes:
    payload = {**ledger, "market": sorted(ledger["market"], key=_TS), "s": {
        dom: sorted(rows, key=_TS) for dom, rows in sorted(ledger["s"].items())
    }}
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    output = io.BytesIO()
    with gzip.GzipFile(fileobj=output, mode="wb", mtime=0) as fh:
        fh.write(raw)
    return output.getvalue()


def save_ledger(ledger: dict, path: Path) -> None:
    data = _ledger_bytes(ledger)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as fh:
        temp_path = Path(fh.name)
        try:
            fh.write(data)
            fh.close()
            temp_path.replace(path)
        finally:
            fh.close()
            temp_path.unlink(missing_ok=True)


def _hour_row(rows: list[list], ts: str, initial: list) -> list:
    pos = bisect_left(rows, ts, key=_TS)
    if pos == len(rows) or rows[pos][0] != ts:
        rows.insert(pos, initial)
    return rows[pos]


def _station_row(ledger: dict, dom: str, ts: str) -> list:
    return _hour_row(ledger["s"].setdefault(dom, []), ts, [ts, None, None, None, [None] * len(TRACKED)])


def merge_fetch_run(ledger: dict, run: dict) -> None:
    ts = hour_ts(run.get("run_at"))
    log = run.get("log")
    if ts is None or not isinstance(log, list):
        return
    for entry in log:
        if not isinstance(entry, dict) or not _domain(entry.get("domain")) or type(entry.get("ok")) is not bool:
            continue
        point = _station_row(ledger, entry["domain"], ts)
        point[1] = int(entry["ok"])
        elapsed_ms = entry.get("elapsed_ms")
        if entry["ok"] and type(elapsed_ms) is int:
            point[2] = elapsed_ms


def merge_history(ledger: dict, snap: dict) -> None:
    ts = hour_ts(snap.get("run_at"))
    quotes = snap.get("q")
    if ts is None or not isinstance(quotes, dict):
        return
    station_prices: dict[str, dict[str, float]] = {}
    for key, quote in quotes.items():
        if not isinstance(key, str) or "|" not in key or not isinstance(quote, list) or len(quote) < 2:
            continue
        dom, model, lane = parse_history_key(key)
        kind, price = quote[:2]
        if kind != "token" or lane != "standard" or not _finite(price) or price <= 0 or not _domain(dom):
            continue
        canon = pricing.canonical_model(model)
        prices = station_prices.setdefault(dom, {})
        prices[canon] = min(prices.get(canon, price), price)

    medians = model_medians(station_prices)
    market = _hour_row(ledger["market"], ts, [ts] + [None] * len(TRACKED))
    for i, model in enumerate(TRACKED, start=1):
        price = _price(medians.get(model))
        if price is not None:
            market[i] = price
    for dom, prices in station_prices.items():
        point = _station_row(ledger, dom, ts)
        try:
            index = market_index(prices, medians)
        except OverflowError:
            index = None
        if _finite(index):
            point[3] = round(index, 1)
        for i, model in enumerate(TRACKED):
            price = _price(prices.get(model))
            if price is not None:
                point[4][i] = price


def prune(ledger: dict, now: datetime) -> None:
    cutoff = _utc(now) - timedelta(days=WINDOW_DAYS)
    cutoff_hour = cutoff.replace(minute=0, second=0, microsecond=0)
    if cutoff != cutoff_hour:
        cutoff_hour += timedelta(hours=1)
    earliest = _hour(cutoff_hour)

    def recent(rows: list[list]) -> list[list]:
        return [row for row in rows if row[0] >= earliest][-MAX_POINTS:]

    ledger["market"] = recent(ledger["market"])
    ledger["s"] = {dom: kept for dom, rows in sorted(ledger["s"].items()) if (kept := recent(rows))}


def _input_files(path: Path, pattern: str) -> list[Path]:
    try:
        return sorted(path.glob(pattern))
    except OSError:
        return []


def _history_time(path: Path) -> datetime | None:
    # The builder names snapshots with their UTC run time. Skip known hours without opening the gzip.
    if _HISTORY_NAME.fullmatch(path.name):
        return _iso_utc(path.name.removesuffix(".json.gz"))
    return None


def update_ledger(data_dir: Path, now: datetime, *, save: bool = True) -> dict:
    path = data_dir / LEDGER_NAME
    ledger = load_ledger(path)
    runs = []
    for source in _input_files(data_dir / "fetch-runs", "*.json"):
        run = _read_json(source)
        dt = _iso_utc(run.get("run_at")) if run is not None else None
        if dt is not None:
            runs.append((dt, run))
    for _, run in sorted(runs, key=_TS):
        merge_fetch_run(ledger, run)

    # Freeze the set of cached hours so multiple new snapshots of one hour can all contribute.
    cached_hours = {row[0] for row in ledger["market"]}
    history = []
    for source in _input_files(data_dir / "history", "*.json.gz"):
        dt = _history_time(source)
        snap = None
        if dt is None:
            snap = _read_json(source, compressed=True)
            dt = _iso_utc(snap.get("run_at")) if snap is not None else None
        if dt is not None:
            history.append((dt, source, snap))
    history.sort(key=_TS)
    for i, (dt, source, snap) in enumerate(history):
        if _hour(dt) in cached_hours and i != len(history) - 1:
            continue
        if snap is None:
            snap = _read_json(source, compressed=True)
        if snap is not None:
            merge_history(ledger, snap)

    prune(ledger, now)
    if save:
        save_ledger(ledger, path)
    return ledger


def station_series(ledger: dict, dom: str) -> list[list]:
    return ledger["s"].get(dom, [])


def market_series(ledger: dict) -> list[list]:
    return ledger["market"]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path(__file__).resolve().parent.parent / "data")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    ledger = update_ledger(args.data, datetime.now(timezone.utc), save=not args.dry_run)
    size = len(_ledger_bytes(ledger)) if args.dry_run else (args.data / LEDGER_NAME).stat().st_size
    print(json.dumps({
        "stations": len(ledger["s"]),
        "points": sum(len(points) for points in ledger["s"].values()),
        "market_rows": len(ledger["market"]),
        "bytes": size,
    }))


if __name__ == "__main__":
    main()
