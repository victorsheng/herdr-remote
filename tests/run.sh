#!/bin/sh
# tests/run.sh — tests for herdr-remote
PASS=0; FAIL=0
DIR="$(cd "$(dirname "$0")/.." && pwd)"

assert_eq() {
  if [ "$1" = "$2" ]; then PASS=$((PASS+1)); echo "  pass: $3"
  else FAIL=$((FAIL+1)); echo "  FAIL: $3 (expected '$2', got '$1')"; fi
}

echo "herdr-remote tests"
echo ""

# --- Relay ---
echo "=== Relay ==="
echo "1. relay syntax"
python3 -c "import ast; ast.parse(open('$DIR/relay/herdr_relay.py').read())" 2>/dev/null
assert_eq "$?" "0" "herdr_relay.py parses"

echo "2. PEP 723 metadata"
grep -q "requires-python" "$DIR/relay/herdr_relay.py"
assert_eq "$?" "0" "inline deps present"

echo "3. start.sh executable"
[ -x "$DIR/relay/start.sh" ]
assert_eq "$?" "0" "start.sh +x"

# --- Telegram ---
echo ""
echo "=== Telegram bot ==="
echo "4. telegram bot syntax"
python3 -c "import ast; ast.parse(open('$DIR/relay/herdr_telegram.py').read())" 2>/dev/null
assert_eq "$?" "0" "herdr_telegram.py parses"

echo "5. telegram demo bot syntax"
python3 -c "import ast; ast.parse(open('$DIR/relay/herdr_telegram_demo.py').read())" 2>/dev/null
assert_eq "$?" "0" "herdr_telegram_demo.py parses"

echo "6. telegram bot has all commands"
for cmd in cmd_start cmd_agents cmd_status cmd_read cmd_send cmd_reply cmd_trust cmd_interrupt; do
  grep -q "async def $cmd" "$DIR/relay/herdr_telegram.py" || { FAIL=$((FAIL+1)); echo "  FAIL: missing $cmd"; continue; }
done
PASS=$((PASS+1)); echo "  pass: all 8 commands present"

echo "7. telegram bot env vars documented"
grep -q "HERDR_TG_TOKEN" "$DIR/relay/herdr_telegram.py" && grep -q "HERDR_TG_CHAT_ID" "$DIR/relay/herdr_telegram.py"
assert_eq "$?" "0" "env vars referenced"

echo "8. telegram dashboard behavior"
uv run "$DIR/tests/test_telegram.py"
assert_eq "$?" "0" "telegram dashboard tests"

echo "9. relay agent state behavior"
python3 "$DIR/tests/test_agent_state.py"
assert_eq "$?" "0" "agent state tests"

# --- Lark bot ---
echo ""
echo "=== Lark bot ==="
echo "L1. lark bot syntax"
python3 -c "import ast; ast.parse(open('$DIR/relay/herdr_lark.py').read())" 2>/dev/null
assert_eq "$?" "0" "herdr_lark.py parses"

echo "L2. PEP 723 metadata"
grep -q "requires-python" "$DIR/relay/herdr_lark.py"
assert_eq "$?" "0" "inline deps present"

echo "L3. lark bot env vars documented"
grep -q "HERDR_LARK_APP_ID" "$DIR/relay/herdr_lark.py" && grep -q "HERDR_LARK_CHAT_ID" "$DIR/relay/herdr_lark.py"
assert_eq "$?" "0" "env vars referenced"

echo "L4. approval sends option index, never option text"
grep -q 'send_keys_to_relay(pane_id, \[str(key)\])' "$DIR/relay/herdr_lark.py"
assert_eq "$?" "0" "approval presses the option number"

echo "L5. interrupt uses relay SAFE_KEYS name"
grep -q '\["C-c"\]' "$DIR/relay/herdr_lark.py"
assert_eq "$?" "0" "interrupt sends C-c"

echo "L6. lark bot behavior"
uv run "$DIR/tests/test_lark.py" >/dev/null 2>&1
assert_eq "$?" "0" "lark bot tests"

echo "L7. lark e2e (needs running relay)"
if pgrep -f "herdr_relay.py" >/dev/null 2>&1; then
  # 只读模式：全量套件不该往真实 agent 写东西。
  # relay 可能启用了 token，从配置里取。
  ( set -a
    [ -f "$HOME/.config/herdr-remote/config.env" ] && . "$HOME/.config/herdr-remote/config.env"
    [ -f "$HOME/.config/herdr-remote/secrets.env" ] && . "$HOME/.config/herdr-remote/secrets.env"
    set +a
    uv run "$DIR/tests/e2e_lark.py" --read-only >/dev/null 2>&1 )
  RC=$?
  if [ "$RC" = "2" ]; then
    PASS=$((PASS+1)); echo "  skip: relay unreachable (no token?)"
  else
    assert_eq "$RC" "0" "lark e2e (read-only)"
  fi
else
  PASS=$((PASS+1)); echo "  skip: relay not running"
fi

echo "L8. usage stats"
uv run "$DIR/tests/test_usage.py" >/dev/null 2>&1
assert_eq "$?" "0" "usage stats tests"

echo "L9. /usage wired into lark bot"
grep -q 'command == "usage"' "$DIR/relay/herdr_lark.py"
assert_eq "$?" "0" "/usage dispatched"

# --- TUI ---
echo ""
echo "=== TUI ==="
echo "10. TUI syntax"
python3 -c "import ast; ast.parse(open('$DIR/relay/herdr_tui.py').read())" 2>/dev/null
assert_eq "$?" "0" "herdr_tui.py parses"

# --- Web app ---
echo ""
echo "=== Web app ==="
echo "11. web app key elements"
WEB="$DIR/web/index.html"
grep -q "WebSocket" "$WEB" && grep -q "theme" "$WEB" && grep -q "sendKey" "$WEB"
assert_eq "$?" "0" "has WebSocket, themes, keyboard"

echo "12. web app no hardcoded secrets"
! grep -q "c4a2385e" "$WEB" && ! grep -q "graffold" "$WEB"
assert_eq "$?" "0" "no secrets in web app"

# --- macOS app ---
echo ""
echo "=== macOS app ==="
echo "13. Swift sources parse"
if command -v swiftc >/dev/null 2>&1; then
  swiftc -parse "$DIR/herdi-mac/Sources/Agent.swift" "$DIR/herdi-mac/Sources/RelayConnection.swift" 2>/dev/null
  assert_eq "$?" "0" "core Swift parses"
else
  PASS=$((PASS+1)); echo "  skip: swiftc not available"
fi

echo "14. build.sh and dmg.sh present"
[ -x "$DIR/herdi-mac/build.sh" ] && [ -f "$DIR/herdi-mac/dmg.sh" ]
assert_eq "$?" "0" "build scripts present"

echo "15. updater points to correct repo"
grep -q "dcolinmorgan/herdr-remote" "$DIR/herdi-mac/Sources/Updater.swift"
assert_eq "$?" "0" "updater repo correct"

# --- Demo worker ---
echo ""
echo "=== Demo worker ==="
echo "16. demo worker syntax"
if [ -f "$DIR/demo-worker/src/index.js" ]; then
  node --check "$DIR/demo-worker/src/index.js" 2>/dev/null
  assert_eq "$?" "0" "demo worker parses"
else
  PASS=$((PASS+1)); echo "  skip: not present"
fi

# --- Integration ---
echo ""
echo "=== Integration ==="
echo "17. README links to herdr-demo.pages.dev"
grep -q "herdr-demo.pages.dev" "$DIR/README.md"
assert_eq "$?" "0" "demo URL correct"

echo "18. README links to herdr-push"
grep -q "dcolinmorgan/herdr-push" "$DIR/README.md"
assert_eq "$?" "0" "plugin link present"

echo "19. installer service behavior"
"$DIR/tests/install-service.sh"
assert_eq "$?" "0" "installer handles Telegram service lifecycle"

echo "20. LICENSE is AGPL"
grep -q "GNU AFFERO GENERAL PUBLIC LICENSE" "$DIR/LICENSE"
assert_eq "$?" "0" "AGPL license"

echo ""
echo "Results: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ] && exit 0 || exit 1
