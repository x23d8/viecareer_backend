"""Check Supabase pooler connectivity for every configured table."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from fastapi.encoders import jsonable_encoder
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from main import app
from services.db_crud_service import TABLES


def main() -> int:
    result = {
        "health": None,
        "tables": [],
        "ok": True,
    }

    client = TestClient(app)
    health_response = client.get("/api/db/_health")
    result["health"] = health_response.json()
    if health_response.status_code != 200:
        result["ok"] = False
        result["health"]["status_code"] = health_response.status_code
        print(json.dumps(jsonable_encoder(result), indent=2))
        return 1

    for table in TABLES:
        item = {"table": table.name, "connected": False, "row_count_sample": 0}
        response = client.get(
            f"/api/db/{table.name}",
            params={"limit": 1, "offset": 0, "order_by": table.primary_key},
        )
        if response.status_code == 200:
            rows = response.json()["data"]
            item["connected"] = True
            item["row_count_sample"] = len(rows)
        else:
            result["ok"] = False
            item["status_code"] = response.status_code
            item["detail"] = response.json()
        result["tables"].append(item)

    print(json.dumps(jsonable_encoder(result), indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
