#!/usr/bin/env bash
# 对运行中的服务演示：更正前核查 → 更正图幅 → 更正后核查。
#   docker compose up --build -d
#   ./scripts/demo.sh
#   BASE=http://localhost:8080 PROJECT=DEMO-REG-001 ./scripts/demo.sh
set -euo pipefail

BASE="${BASE:-http://localhost:8000}"
PROJECT="${PROJECT:-DEMO-REG-001}"
SHEETS="${SHEETS:-C 0 0 57 100}"
REFERENCE="${REFERENCE:-A}"
TARGET="${TARGET:-C}"

say() { printf '\n== %s ==\n' "$1"; }

version() {
  curl -fsS "$BASE/api/projects/$PROJECT/state" |
    python3 -c 'import json,sys; print(json.load(sys.stdin)["project"]["version"])'
}

verify() {
  curl -fsS -X POST "$BASE/api/projects/$PROJECT/verify" \
    -H 'Content-Type: application/json' \
    -d "{\"reference\":\"$REFERENCE\",\"target\":\"$TARGET\"}" |
    python3 -c 'import json,sys
v = json.load(sys.stdin)["verification"]
print("状态:", v["status_label"], "（" + v["status"] + "）")
print("说明:", v["message"])
if v["translation"]:
    print("平移量:", v["translation"])
for index, path in enumerate(v["paths"], 1):
    print("  路径%d: 测量 %s 经图幅 %s" % (index, path["measurements"], path["sheets"]))
for item in v["invalid"]:
    print("  无效测量:", item["no"], "->", "；".join(item["reasons"]))'
}

say "健康检查"
curl -fsS "$BASE/healthz"; echo

say "更正前：基准片 $REFERENCE → 待配准片 $TARGET"
verify

say "更正图幅：$SHEETS（测量原坐标不变）"
python3 - "$SHEETS" "$(version)" "$BASE" "$PROJECT" <<'PY'
import json, sys, urllib.request
text, version, base, project = sys.argv[1:]
sheets = []
for line in text.splitlines():
    parts = line.split()
    if not parts:
        continue
    sheets.append([parts[0], *map(int, parts[1:])])
body = json.dumps({"version": int(version), "sheets": sheets}).encode()
request = urllib.request.Request(
    f"{base}/api/projects/{project}/correct",
    data=body,
    headers={"Content-Type": "application/json"},
    method="POST",
)
with urllib.request.urlopen(request) as response:
    print(json.loads(response.read())["message"])
PY

say "更正后（同一份测量表，按新图幅重判有效性）"
verify

say "演示完成：打开 $BASE/projects/$PROJECT?reference=$REFERENCE&target=$TARGET 查看页面"
