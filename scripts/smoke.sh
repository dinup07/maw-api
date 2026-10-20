#!/usr/bin/env bash
# Smoke-test a running MAW server.
#   ./scripts/smoke.sh                       # http://localhost:8000
#   BASE=https://1-2-3-4.sslip.io API_KEY=... ./scripts/smoke.sh
# Real-model checks run when the server reports the docs_gemini / docs_openai stores and REAL=1.
set -uo pipefail
BASE="${BASE:-http://localhost:8000}"
AUTH=()
[ -n "${API_KEY:-}" ] && AUTH=(-H "X-API-Key: ${API_KEY}")
J='content-type: application/json'
pass=0; fail=0

check() {  # check NAME EXPECTED_SUBSTRING CURL_ARGS...
  local name="$1" expected="$2"; shift 2
  local body
  body=$(curl -sS -m 120 "${AUTH[@]}" "$@" 2>&1)
  if grep -q -- "$expected" <<<"$body"; then
    echo "  ok    $name"; pass=$((pass + 1))
  else
    echo "  FAIL  $name"; echo "        expected: $expected"; echo "        got: ${body:0:300}"; fail=$((fail + 1))
  fi
}

run_workflow() {  # run_workflow FILE INPUT -> JSON body for POST /v1/runs
  python3 -c 'import json, sys; print(json.dumps({"workflow": json.load(open(sys.argv[1])), "input": sys.argv[2]}))' "$1" "$2"
}

echo "MAW smoke test against $BASE"
check "health"                '"status":"ok"'    "$BASE/health"
check "ready"                 '"status":"ready"' "$BASE/ready"
check "catalog listed"        'support_triage'   "$BASE/v1/workflows"
check "hello (scripted)"      'echo: hi'         "$BASE/v1/runs" -H "$J" -d '{"workflow_id": "hello", "input": "hi"}'
check "triage -> A2A agent"   'refund'           "$BASE/v1/runs" -H "$J" -d '{"workflow_id": "support_triage", "input": "Was INV-1001 charged twice?"}'
check "triage -> MCP tools"   'self-service'     "$BASE/v1/runs" -H "$J" -d '{"workflow_id": "support_triage", "input": "my vpn password expired"}'
check "parallel join"         'Reset your VPN'   "$BASE/v1/runs" -H "$J" -d '{"workflow_id": "kb_research", "input": "vpn password reset"}'

if curl -sS -m 10 "${AUTH[@]}" "$BASE/v1/vector-stores/docs" | grep -q '"chunks"'; then
  check "RAG (scripted docs)" 'annual-leave.md' "$BASE/v1/runs" -H "$J" -d "$(run_workflow examples/rag_qa.json 'how many days of annual leave do I get')"
else
  echo "  skip  RAG: the docs store needs MAW_DATABASE_URL and \`make ingest\`"
fi

if [ "${REAL:-0}" = "1" ]; then
  check "Gemini RAG"  '24' "$BASE/v1/runs" -H "$J" -d "$(run_workflow examples/rag_qa_gemini.json 'How many days of annual leave do full-time staff get?')"
  check "OpenAI RAG"  '24' "$BASE/v1/runs" -H "$J" -d "$(run_workflow examples/rag_qa_openai.json 'How many days of annual leave do full-time staff get?')"
  check "Gemini triage" '"status":"completed"' "$BASE/v1/runs" -H "$J" -d "$(run_workflow examples/gemini_support_triage.json 'my vpn password expired')"
else
  echo "  skip  real-model checks (run with REAL=1 after: make ingest STORE=docs_gemini; make ingest STORE=docs_openai)"
fi

echo "$pass passed, $fail failed"
[ "$fail" -eq 0 ]
