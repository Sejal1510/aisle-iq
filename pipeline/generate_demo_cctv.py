# pipeline/generate_demo_cctv.py
"""
Deterministic synthetic CCTV demo-event generator.

Produces ``data/demo_cctv_events_st1001_st1002.jsonl``: a two-day, multi-hour,
entirely synthetic CanonicalEvent stream for ST1001/ST1002 built specifically
so every P3 analytics endpoint (occupancy, live queue, hourly footfall,
peak-hour ranking, period comparison) has something meaningful to show.

This file is separate from, and never modifies, ``data/generated_cctv_events.jsonl``
-- the mandatory challenge deliverable produced by the real YOLOv8/ByteTrack
video pipeline. That file predates the current queue-lifecycle model (it has
no BILLING_QUEUE_JOIN/COMPLETE events at all) and spans about two minutes of
simulated time, which is not enough to demonstrate occupancy history, hourly
variation, or a real queue state -- this generator exists to fill that gap
for demo purposes, the same way ``pipeline/generate_demo_pos.py`` fills the
POS-alignment gap. Every id in the output is prefixed ``DEMO-`` so it is
unmistakably synthetic.

No source video, model weights, or external service is used. Generation is
pure Python, driven by a single seeded ``random.Random`` instance -- the same
seed and store profiles always produce byte-identical output.

Design notes (see also docs added to README):

* All events use the CanonicalEvent schema (uppercase event types, top-level
  ``store_id``/``camera_id``/``visitor_id``), matching exactly what the real
  video pipeline (``pipeline/video/events.py``) emits -- no ingestion code
  is special-cased for this data.
* CanonicalEvent identity resolution is scoped to (store_id, camera_id,
  "visitor_id", visitor_id) -- see EventIngestionService._resolve_tracked_entity.
  Each synthetic shopper is therefore assigned ONE fixed camera_id (a
  "tracking channel", drawn from a small per-store pool) used for every event
  in their visit, so their whole journey resolves to a single canonical
  TrackedEntity with one cleanly opened-and-closed VisitSession. A real
  multi-camera pipeline would fragment identity across physical cameras
  (the documented P2 no-cross-camera-ReID limitation); this generator
  authors a clean ground-truth journey directly instead, which is the
  correct thing for synthetic demo data to do.
* A small number of shoppers in the final simulated hour are deliberately
  left mid-visit -- joined the queue, no completion/abandonment event, no
  exit -- so "current" queue/occupancy endpoints have something live to show
  when queried with no explicit ``as_of`` (which resolves to the latest
  ingested event timestamp for that store).
"""
from __future__ import annotations

import argparse
import json
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT_PATH = PROJECT_ROOT / "data" / "demo_cctv_events_st1001_st1002.jsonl"
DEFAULT_SEED = 20260601

OPERATING_HOURS = range(9, 20)  # 09:00 - 19:59, matches both store profiles
DAY_DATES = (datetime(2026, 6, 1), datetime(2026, 6, 2))  # yesterday, today
DAY_VOLUME_MULTIPLIERS = (1.0, 1.25)  # today is busier than yesterday, deterministically


@dataclass(frozen=True)
class ZoneSpec:
    zone_id: str
    name: str
    zone_type: str
    is_revenue_zone: bool


@dataclass(frozen=True)
class StoreTrafficProfile:
    store_id: str
    label: str
    hourly_weights: dict[int, float]
    base_arrivals_per_hour: float
    queue_join_rate: float
    queue_abandon_rate: float
    zones: tuple[ZoneSpec, ...]
    camera_pool_size: int


DEFAULT_PROFILES: dict[str, StoreTrafficProfile] = {
    "ST1001": StoreTrafficProfile(
        store_id="ST1001",
        label="mall flagship -- single lunch-hour peak",
        hourly_weights={
            9: 0.3, 10: 0.5, 11: 0.8, 12: 1.6, 13: 1.8, 14: 1.2,
            15: 0.7, 16: 0.6, 17: 0.8, 18: 1.1, 19: 0.6,
        },
        base_arrivals_per_hour=6.0,
        queue_join_rate=0.6,
        queue_abandon_rate=0.18,
        zones=(
            ZoneSpec("ST1001_MAKEUP", "Makeup", "DISPLAY", True),
            ZoneSpec("ST1001_SKINCARE", "Skincare", "SHELF", True),
            ZoneSpec("ST1001_FRAGRANCE", "Fragrance", "DISPLAY", True),
        ),
        camera_pool_size=3,
    ),
    "ST1002": StoreTrafficProfile(
        store_id="ST1002",
        label="transit-adjacent -- morning + evening commute peaks",
        hourly_weights={
            9: 1.7, 10: 0.9, 11: 0.5, 12: 0.6, 13: 0.5, 14: 0.4,
            15: 0.5, 16: 0.7, 17: 1.3, 18: 1.9, 19: 1.0,
        },
        base_arrivals_per_hour=4.0,
        queue_join_rate=0.45,
        queue_abandon_rate=0.22,
        zones=(
            ZoneSpec("ST1002_MENS", "Mens", "SHELF", True),
            ZoneSpec("ST1002_ACCESSORIES", "Accessories", "DISPLAY", False),
        ),
        camera_pool_size=2,
    ),
}

# Shoppers who arrive in this window on the last simulated day are eligible to
# be left "still queued" (join, no terminal, no exit) -- see generate_demo_cctv_events.
LIVE_QUEUE_WINDOW_HOUR = OPERATING_HOURS[-1]
LIVE_QUEUE_COUNT = 2


@dataclass
class DemoCctvGenerationResult:
    output_path: Path
    events: list[dict] = field(default_factory=list)

    @property
    def event_count(self) -> int:
        return len(self.events)

    @property
    def counts_by_store_and_type(self) -> dict[str, dict[str, int]]:
        counts: dict[str, dict[str, int]] = {}
        for event in self.events:
            store_counts = counts.setdefault(event["store_id"], {})
            store_counts[event["event_type"]] = store_counts.get(event["event_type"], 0) + 1
        return counts


def generate_demo_cctv_events(
    *,
    seed: int = DEFAULT_SEED,
    profiles: dict[str, StoreTrafficProfile] | None = None,
    day_dates: tuple[datetime, ...] = DAY_DATES,
    day_multipliers: tuple[float, ...] = DAY_VOLUME_MULTIPLIERS,
) -> list[dict]:
    """Generate the full synthetic event set, chronologically ordered.

    Deterministic: a fresh ``random.Random(seed)`` instance is created here
    and threaded through every helper explicitly -- nothing reads Python's
    global ``random`` state or wall-clock time, so the same ``seed`` and
    ``profiles``/``day_dates``/``day_multipliers`` always produce the same
    output, regardless of call order or what else has run in the process.
    """
    profiles = profiles or DEFAULT_PROFILES
    rng = random.Random(seed)

    events: list[dict] = []
    shopper_seq = 0

    for profile in profiles.values():
        for day_index, day_date in enumerate(day_dates):
            multiplier = day_multipliers[day_index] if day_index < len(day_multipliers) else 1.0
            is_last_day = day_index == len(day_dates) - 1

            for hour in OPERATING_HOURS:
                arrivals = _stochastic_round(
                    profile.base_arrivals_per_hour * profile.hourly_weights.get(hour, 0.5) * multiplier,
                    rng,
                )
                is_live_hour = is_last_day and hour == LIVE_QUEUE_WINDOW_HOUR
                # Reserve the last couple of arrival slots in the live hour to
                # be left mid-queue-visit -- computed against this hour's
                # actual arrival count, not a fixed guess, so it still fires
                # even on a quiet closing hour.
                live_indices = (
                    set(range(max(0, arrivals - LIVE_QUEUE_COUNT), arrivals)) if is_live_hour else set()
                )

                for arrival_index in range(arrivals):
                    shopper_seq += 1
                    force_live = arrival_index in live_indices
                    events.extend(
                        _build_shopper_journey(
                            profile=profile,
                            day_date=day_date,
                            hour=hour,
                            shopper_seq=shopper_seq,
                            rng=rng,
                            leave_queue_active=force_live,
                        )
                    )

    events.sort(key=lambda event: event["timestamp"])
    return events


def _stochastic_round(value: float, rng: random.Random) -> int:
    whole = int(value)
    fraction = value - whole
    return whole + (1 if rng.random() < fraction else 0)


def _build_shopper_journey(
    *,
    profile: StoreTrafficProfile,
    day_date: datetime,
    hour: int,
    shopper_seq: int,
    rng: random.Random,
    leave_queue_active: bool,
) -> list[dict]:
    store_id = profile.store_id
    visitor_id = f"DEMO-{store_id}-V{shopper_seq:04d}"
    camera_id = f"{store_id}_CAM_TRACK_{(shopper_seq % profile.camera_pool_size) + 1}"

    arrival_offset = timedelta(minutes=rng.randint(0, 59), seconds=rng.randint(0, 59))
    cursor = day_date.replace(hour=hour, minute=0, second=0, microsecond=0) + arrival_offset

    events: list[dict] = []
    event_ordinal = 0

    def next_event_id(kind: str) -> str:
        nonlocal event_ordinal
        event_ordinal += 1
        return f"DEMO-{store_id}-{shopper_seq:04d}-{event_ordinal:02d}-{kind}"

    def confidence() -> float:
        return round(rng.uniform(0.76, 0.97), 4)

    # 1. ENTRY
    events.append(
        _canonical_event(
            event_id=next_event_id("ENTRY"),
            store_id=store_id,
            camera_id=camera_id,
            visitor_id=visitor_id,
            event_type="ENTRY",
            timestamp=cursor,
            confidence=confidence(),
        )
    )

    # 2. Zero, one, or two zone visits
    zone_visit_roll = rng.random()
    zone_count = 0 if zone_visit_roll < 0.2 else (1 if zone_visit_roll < 0.7 else 2)
    visited_zones = rng.sample(profile.zones, k=min(zone_count, len(profile.zones)))

    cursor += timedelta(seconds=rng.randint(10, 90))
    for zone in visited_zones:
        hotspot = (round(rng.uniform(0.1, 0.9), 4), round(rng.uniform(0.1, 0.9), 4))
        events.append(
            _canonical_event(
                event_id=next_event_id("ZONE-ENTER"),
                store_id=store_id,
                camera_id=camera_id,
                visitor_id=visitor_id,
                event_type="ZONE_ENTER",
                timestamp=cursor,
                confidence=confidence(),
                zone_id=zone.zone_id,
                metadata={
                    "sku_zone": zone.name,
                    "zone_type": zone.zone_type,
                    "is_revenue_zone": "Yes" if zone.is_revenue_zone else "No",
                    "hotspot_x": hotspot[0],
                    "hotspot_y": hotspot[1],
                },
            )
        )
        dwell_seconds = rng.randint(60, 360)
        cursor += timedelta(seconds=dwell_seconds)
        events.append(
            _canonical_event(
                event_id=next_event_id("ZONE-EXIT"),
                store_id=store_id,
                camera_id=camera_id,
                visitor_id=visitor_id,
                event_type="ZONE_EXIT",
                timestamp=cursor,
                confidence=confidence(),
                zone_id=zone.zone_id,
                metadata={
                    "sku_zone": zone.name,
                    "zone_type": zone.zone_type,
                    "is_revenue_zone": "Yes" if zone.is_revenue_zone else "No",
                    "hotspot_x": hotspot[0],
                    "hotspot_y": hotspot[1],
                },
            )
        )
        cursor += timedelta(seconds=rng.randint(10, 60))

    # 3. Queue visit (join, then complete/abandon -- unless deliberately left active).
    # Shoppers picked to be left mid-visit always join, overriding the random
    # roll, so the "at least one active queue state" guarantee is unconditional.
    joins_queue = leave_queue_active or rng.random() < profile.queue_join_rate
    if joins_queue:
        cursor += timedelta(seconds=rng.randint(20, 120))
        queue_event_id = f"DEMO-{store_id}-{shopper_seq:04d}-Q"
        queue_zone_id = f"{store_id}_BILLING_QUEUE"
        joined_at = cursor
        queue_depth = rng.randint(1, 4)
        hotspot = (round(rng.uniform(0.4, 0.6), 4), round(rng.uniform(0.7, 0.95), 4))

        events.append(
            _canonical_event(
                event_id=next_event_id("QUEUE-JOIN"),
                store_id=store_id,
                camera_id=camera_id,
                visitor_id=visitor_id,
                event_type="BILLING_QUEUE_JOIN",
                timestamp=joined_at,
                confidence=confidence(),
                zone_id=queue_zone_id,
                metadata={
                    "queue_event_id": queue_event_id,
                    "queue_depth": queue_depth,
                    "queue_join_ts": joined_at.isoformat(),
                    "sku_zone": "Billing Queue",
                    "zone_type": "BILLING",
                    "is_revenue_zone": "Yes",
                    "hotspot_x": hotspot[0],
                    "hotspot_y": hotspot[1],
                },
            )
        )

        if not leave_queue_active:
            abandons = rng.random() < profile.queue_abandon_rate
            wait_seconds = rng.randint(15, 90) if abandons else rng.randint(60, 600)
            cursor = joined_at + timedelta(seconds=wait_seconds)
            events.append(
                _canonical_event(
                    event_id=next_event_id("QUEUE-ABANDON" if abandons else "QUEUE-COMPLETE"),
                    store_id=store_id,
                    camera_id=camera_id,
                    visitor_id=visitor_id,
                    event_type="BILLING_QUEUE_ABANDON" if abandons else "BILLING_QUEUE_COMPLETE",
                    timestamp=cursor,
                    confidence=0.8,
                    zone_id=queue_zone_id,
                    dwell_ms=wait_seconds * 1000,
                    metadata={
                        "queue_event_id": queue_event_id,
                        "queue_depth": queue_depth,
                        "queue_join_ts": joined_at.isoformat(),
                        "queue_served_ts": None,
                        "queue_exit_ts": cursor.isoformat(),
                        "wait_seconds": wait_seconds,
                        "queue_position_at_join": queue_depth,
                        "abandoned": abandons,
                        "sku_zone": "Billing Queue",
                        "zone_type": "BILLING",
                        "is_revenue_zone": "Yes",
                        "hotspot_x": hotspot[0],
                        "hotspot_y": hotspot[1],
                    },
                )
            )
        else:
            # Deliberately incomplete: still waiting as of the dataset's own
            # "now" -- no terminal event, no EXIT below. This is what makes
            # queue/current and occupancy/current nonzero out of the box.
            return events

    # 4. EXIT (skipped above for shoppers deliberately left mid-visit)
    cursor += timedelta(seconds=rng.randint(20, 150))
    events.append(
        _canonical_event(
            event_id=next_event_id("EXIT"),
            store_id=store_id,
            camera_id=camera_id,
            visitor_id=visitor_id,
            event_type="EXIT",
            timestamp=cursor,
            confidence=confidence(),
        )
    )

    return events


def _canonical_event(
    *,
    event_id: str,
    store_id: str,
    camera_id: str,
    visitor_id: str,
    event_type: str,
    timestamp: datetime,
    confidence: float,
    zone_id: str | None = None,
    dwell_ms: int = 0,
    metadata: dict | None = None,
) -> dict:
    return {
        "event_id": event_id,
        "store_id": store_id,
        "camera_id": camera_id,
        "visitor_id": visitor_id,
        "event_type": event_type,
        "timestamp": timestamp.isoformat(),
        "zone_id": zone_id,
        "dwell_ms": dwell_ms,
        "is_staff": False,
        "confidence": confidence,
        "metadata": {
            "queue_depth": None,
            "session_seq": None,
            **(metadata or {}),
        },
    }


def write_demo_cctv_jsonl(events: list[dict], output_path: Path = DEFAULT_OUTPUT_PATH) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as jsonl_file:
        for event in events:
            jsonl_file.write(json.dumps(event, separators=(",", ":")))
            jsonl_file.write("\n")


def _result_payload(result: DemoCctvGenerationResult) -> dict[str, object]:
    return {
        "output_path": str(result.output_path),
        "events": result.event_count,
        "counts_by_store_and_type": result.counts_by_store_and_type,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate deterministic synthetic CCTV demo events for ST1001/ST1002.")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_PATH)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    args = parser.parse_args()

    events = generate_demo_cctv_events(seed=args.seed)
    write_demo_cctv_jsonl(events, output_path=args.output)
    result = DemoCctvGenerationResult(output_path=args.output, events=events)

    print(json.dumps(_result_payload(result), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
