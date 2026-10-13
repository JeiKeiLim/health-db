"""Split the full-history HAE backfill JSON into <=CHUNK_LIMIT byte v2-envelope chunks.

Lanes are packed independently so every chunk stays a valid payload:
  data.metrics + data.workouts per chunk (the lanes HAE batch-requests),
  then data.ecg sessions in small groups, then data.heartRateNotifications.

Chunks are written to the given output dir; nothing is posted here.
"""

import json
import sys
from pathlib import Path

CHUNK_LIMIT = 60 * 1024 * 1024  # stay under the service's 64 MiB cap


def envelope(lanes: dict) -> bytes:
    return json.dumps({"data": lanes}, ensure_ascii=False).encode()


def pack(items, limit, key):
    """Greedy-pack `items` into envelopes <= limit bytes."""
    chunks = []
    cur, cur_size = [], 0
    for item in items:
        size = len(json.dumps(item, ensure_ascii=False)) + 4
        if cur and cur_size + size > limit:
            chunks.append(cur)
            cur, cur_size = [], 0
        cur.append(item)
        cur_size += size
    if cur:
        chunks.append(cur)
    return [(key, c) for c in chunks]


def main(src: str, outdir: str) -> int:
    out = Path(outdir)
    out.mkdir(parents=True, exist_ok=True)
    doc = json.load(open(src))
    data = doc["data"]

    plans = []
    plans += pack(data.get("metrics", []), CHUNK_LIMIT, "metrics")
    plans += pack(data.get("workouts", []), CHUNK_LIMIT, "workouts")
    plans += pack(data.get("ecg", []), CHUNK_LIMIT, "ecg")
    plans += pack(
        data.get("heartRateNotifications", []), CHUNK_LIMIT, "heartRateNotifications"
    )

    total = 0
    for i, (key, items) in enumerate(plans, 1):
        body = envelope({key: items})
        assert len(body) <= 64 * 1024 * 1024, f"chunk {i} over cap: {len(body)}"
        path = out / f"backfill-chunk-{i:03d}-{key}.json"
        path.write_bytes(body)
        total += len(body)
        print(f"{path.name}: {key} x{len(items)} = {len(body)/1e6:.1f} MB")
    print(f"TOTAL: {len(plans)} chunks, {total/1e6:.1f} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2]))
