#!/usr/bin/env bash
# 演示接幅核查与更正（对着已启动的服务运行）。
#   docker compose up --build -d
#   ./scripts/demo.sh                 # 默认 http://localhost:8000
#   BASE=http://localhost:8080 PROJECT=DEMO-ADJ-001 ./scripts/demo.sh
set -euo pipefail

BASE="${BASE:-http://localhost:8000}"
PROJECT="${PROJECT:-DEMO-ADJ-001}"

say() { printf '\n== %s ==\n' "$1"; }

version() {
  curl -fsS "$BASE/api/projects/$PROJECT/state" |
    python3 -c 'import json,sys; print(json.load(sys.stdin)["project"]["version"])'
}

show_state() {
  curl -fsS "$BASE/api/projects/$PROJECT/state" |
    python3 -c 'import json,sys
d = json.load(sys.stdin)
print("版本:", d["project"]["version"], "统计:", d["stats"])
for i in d["issues"]:
    print(" ", i["kind"], i["rect"], "涉及图幅:", i["sheet_ids"] or "-")
print("  图幅:", [s["no"] for s in d["sheets"]])'
}

say "健康检查"
curl -fsS "$BASE/healthz"; echo

say "初始核查结果"
show_state

say "退回示例：只把 S3 右移到 1050~1150（会在新位置留下缺片）"
curl -fsS -X POST "$BASE/api/projects/$PROJECT/check" \
  -H 'Content-Type: application/json' \
  -d "{\"batch\":[{\"no\":\"S3\",\"x0\":1050,\"y0\":0,\"x1\":1150,\"y1\":500}],\"version\":$(version)}" |
  python3 -c 'import json,sys
d = json.load(sys.stdin)
print("通过:", d["accepted"])
for r in d["reasons"]:
    print(" -", r)'
echo "退回后（应保持不变）："
show_state

say "更正示例：S3 填上段缝 + 新增 S5 填下段缝（预检）"
curl -fsS -X POST "$BASE/api/projects/$PROJECT/check" \
  -H 'Content-Type: application/json' \
  -d "{\"batch\":[{\"no\":\"S3\",\"x0\":1000,\"y0\":0,\"x1\":1100,\"y1\":500},
                  {\"no\":\"S5\",\"x0\":1000,\"y0\":500,\"x1\":1100,\"y1\":1000}],\"version\":$(version)}" |
  python3 -c 'import json,sys
d = json.load(sys.stdin)
print("通过:", d["accepted"], "|", "；".join(d["reasons"]))
print("重算窗口:", d["window"])
print("消除:", d["eliminated"])
print("新位置问题:", d["new_problems"])'

say "写入更正"
curl -fsS -X POST "$BASE/api/projects/$PROJECT/apply" \
  -H 'Content-Type: application/json' \
  -d "{\"batch\":[{\"no\":\"S3\",\"x0\":1000,\"y0\":0,\"x1\":1100,\"y1\":500},
                  {\"no\":\"S5\",\"x0\":1000,\"y0\":500,\"x1\":1100,\"y1\":1000}],\"version\":$(version)}" |
  python3 -c 'import json,sys; d=json.load(sys.stdin); print("已写入:", d["applied"], "|", d["message"])'

say "更正后结果"
show_state

say "过期版本提交（应被拒绝且不写入）"
curl -sS -X POST "$BASE/api/projects/$PROJECT/apply" \
  -H 'Content-Type: application/json' \
  -d '{"batch":[{"no":"S3","x0":1000,"y0":0,"x1":1100,"y1":500}],"version":0}' \
  -o /tmp/adjcheck_stale.json -w 'HTTP %{http_code}\n'
python3 -c 'import json; print(json.load(open("/tmp/adjcheck_stale.json"))["message"])'

say "演示完成：打开 $BASE/projects/$PROJECT 查看页面"
