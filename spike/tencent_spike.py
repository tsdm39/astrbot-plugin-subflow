"""
Tencent Doc smart sheet feasibility spike.

Uses only Python stdlib (urllib + json). No pip install required.

Configure via spike/.env.spike (gitignored). Required keys:
    TENCENT_DOC_CLIENT_ID
    TENCENT_DOC_OPEN_ID
    TENCENT_DOC_ACCESS_TOKEN
    TENCENT_DOC_FILE_ID
    TENCENT_DOC_SHEET_ID

Run from the spike/ directory:
    python tencent_spike.py fields
    python tencent_spike.py records [limit]
    python tencent_spike.py add
    python tencent_spike.py update <recordID>
    python tencent_spike.py delete <recordID>
    python tencent_spike.py smoke      # add -> read-back -> update -> delete
"""

import json
import os
import sys
import urllib.request
import urllib.error
from pathlib import Path
from datetime import datetime

API_BASE = "https://docs.qq.com"
ENDPOINT_TPL = "/openapi/smartbook/v2/files/{file_id}/sheets/{sheet_id}"


def load_env() -> dict:
    env_path = Path(__file__).parent / ".env.spike"
    if not env_path.exists():
        sys.exit(f"missing config: {env_path}\nCopy from .env.spike.example and fill in.")
    env = {}
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip()
    required = [
        "TENCENT_DOC_CLIENT_ID",
        "TENCENT_DOC_OPEN_ID",
        "TENCENT_DOC_ACCESS_TOKEN",
        "TENCENT_DOC_FILE_ID",
        "TENCENT_DOC_SHEET_ID",
    ]
    missing = [k for k in required if not env.get(k)]
    if missing:
        sys.exit(f"missing keys in .env.spike: {missing}")
    return env


def call(env: dict, body: dict) -> dict:
    url = API_BASE + ENDPOINT_TPL.format(
        file_id=env["TENCENT_DOC_FILE_ID"],
        sheet_id=env["TENCENT_DOC_SHEET_ID"],
    )
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    headers = {
        "Access-Token": env["TENCENT_DOC_ACCESS_TOKEN"],
        "Client-Id": env["TENCENT_DOC_CLIENT_ID"],
        "Open-Id": env["TENCENT_DOC_OPEN_ID"],
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    print(f"\n>>> POST {url}")
    print(f">>> body: {json.dumps(body, ensure_ascii=False)}")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode("utf-8")
            status = resp.status
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        status = e.code
        print(f"<<< HTTP {status} (error)")
        print(raw)
        try:
            return json.loads(raw)
        except Exception:
            return {"ret": -1, "msg": f"HTTP {status}", "raw": raw}
    print(f"<<< HTTP {status}")
    parsed = json.loads(raw)
    Path(__file__).parent.joinpath("last_response.json").write_text(
        json.dumps(parsed, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(parsed, ensure_ascii=False, indent=2)[:2000])
    if len(json.dumps(parsed, ensure_ascii=False, indent=2)) > 2000:
        print("... (truncated; full response saved to spike/last_response.json)")
    return parsed


def cmd_fields(env: dict) -> None:
    call(env, {"getFields": {"offset": 0, "limit": 50}})


def cmd_records(env: dict, limit: int = 10) -> None:
    call(env, {"getRecords": {"offset": 0, "limit": limit}})


def _test_row_values() -> dict:
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return {
        "类型": [{"text": "翻译"}],
        "项目": [{"type": "text", "text": f"SPIKE-99 P1（spike@{stamp}）"}],
        "组员": [{"type": "text", "text": "999999999"}],
        "进度": [{"text": "未分配"}],
        "备注": [{"type": "text", "text": f"spike test {stamp}"}],
    }


def cmd_add(env: dict) -> str | None:
    resp = call(
        env,
        {
            "addRecords": {
                "records": [{"values": _test_row_values()}]
            }
        },
    )
    if resp.get("ret") != 0:
        return None
    rid = resp["data"]["addRecords"]["records"][0]["recordID"]
    print(f"\n[ok] inserted recordID = {rid}")
    return rid


def cmd_update(env: dict, record_id: str) -> None:
    call(
        env,
        {
            "updateRecords": {
                "records": [
                    {
                        "recordID": record_id,
                        "values": {
                            "备注": [{"type": "text", "text": f"spike-updated @ {datetime.now().isoformat(timespec='seconds')}"}],
                            "进度": [{"text": "已完成"}],
                        },
                    }
                ]
            }
        },
    )


def cmd_delete(env: dict, record_id: str) -> None:
    call(env, {"deleteRecords": {"recordIDs": [record_id]}})


def cmd_smoke(env: dict) -> None:
    print("=" * 60)
    print("STEP 1/5  list fields (sanity-check schema)")
    print("=" * 60)
    cmd_fields(env)

    print("\n" + "=" * 60)
    print("STEP 2/5  list current records (before)")
    print("=" * 60)
    cmd_records(env, limit=5)

    print("\n" + "=" * 60)
    print("STEP 3/5  add a test record")
    print("=" * 60)
    rid = cmd_add(env)
    if not rid:
        print("\n[fail] add did not return a recordID — stopping smoke test")
        return

    print("\n" + "=" * 60)
    print(f"STEP 4/5  update record {rid}")
    print("=" * 60)
    cmd_update(env, rid)

    print("\n" + "=" * 60)
    print(f"STEP 5/5  delete record {rid}")
    print("=" * 60)
    cmd_delete(env, rid)

    print("\n[done] full add -> update -> delete cycle completed")


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    env = load_env()
    cmd = sys.argv[1]
    args = sys.argv[2:]
    if cmd == "fields":
        cmd_fields(env)
    elif cmd == "records":
        cmd_records(env, int(args[0]) if args else 10)
    elif cmd == "add":
        cmd_add(env)
    elif cmd == "update":
        if not args:
            sys.exit("usage: update <recordID>")
        cmd_update(env, args[0])
    elif cmd == "delete":
        if not args:
            sys.exit("usage: delete <recordID>")
        cmd_delete(env, args[0])
    elif cmd == "smoke":
        cmd_smoke(env)
    else:
        print(__doc__)
        sys.exit(f"unknown command: {cmd}")


if __name__ == "__main__":
    main()
