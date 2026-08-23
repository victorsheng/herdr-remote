#!/bin/sh
# install-lark-service.sh — 把飞书客户端装成开机自启的用户服务。
#
# 沿用 install-service.sh 的约定：配置在 ~/.config/herdr-remote/{config,secrets}.env，
# 日志在 HERDR_LOG_DIR，macOS 用 LaunchAgent、Linux 用 systemd user unit。
#
#   ./install-lark-service.sh            安装并启动
#   ./install-lark-service.sh status     查看状态
#   ./install-lark-service.sh restart    重启
#   ./install-lark-service.sh uninstall  卸载
set -eu

LABEL="com.herdr-remote.lark"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CONFIG_DIR="$HOME/.config/herdr-remote"
CONFIG_FILE="$CONFIG_DIR/config.env"
SECRETS_FILE="$CONFIG_DIR/secrets.env"

case "$(uname -s)" in
    Darwin) OS="macos" ;;
    Linux)  OS="linux" ;;
    *) echo "Unsupported OS: $(uname -s)" >&2; exit 1 ;;
esac

PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
UNIT_DIR="$HOME/.config/systemd/user"
UNIT="$UNIT_DIR/herdr-lark.service"

# --- 通用 ---

load_config() {
    # shellcheck disable=SC1090
    [ -f "$CONFIG_FILE" ] && { set -a; . "$CONFIG_FILE"; set +a; }
    # shellcheck disable=SC1090
    [ -f "$SECRETS_FILE" ] && { set -a; . "$SECRETS_FILE"; set +a; }
    LOG_DIR="${HERDR_LOG_DIR:-$HOME/Library/Logs/herdr-remote}"
    [ "$OS" = "linux" ] && LOG_DIR="${HERDR_LOG_DIR:-$HOME/.local/state/herdr-remote}"
    UV_PATH="${HERDR_UV_PATH:-$(command -v uv || true)}"
    RELAY_DIR="${HERDR_RELAY_DIR:-$SCRIPT_DIR}"
}

is_installed() {
    [ "$OS" = "macos" ] && [ -f "$PLIST" ] && return 0
    [ "$OS" = "linux" ] && [ -f "$UNIT" ] && return 0
    return 1
}

stop_service() {
    if [ "$OS" = "macos" ]; then
        launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
    else
        systemctl --user stop herdr-lark.service 2>/dev/null || true
    fi
}

start_service() {
    if [ "$OS" = "macos" ]; then
        launchctl bootstrap "gui/$(id -u)" "$PLIST"
    else
        systemctl --user daemon-reload
        systemctl --user enable --now herdr-lark.service
    fi
}

# --- 子命令 ---

cmd_status() {
    load_config
    if ! is_installed; then
        echo "未安装。运行 $0 安装。"
        exit 1
    fi
    if [ "$OS" = "macos" ]; then
        launchctl print "gui/$(id -u)/$LABEL" 2>/dev/null \
            | grep -E "state|pid|last exit" || echo "服务未运行"
    else
        systemctl --user status herdr-lark.service --no-pager || true
    fi
    echo ""
    # Python 的 logging 默认写 stderr，所以日志都在 lark-stderr.log
    echo "日志: $LOG_DIR/lark-stderr.log"
    echo "只看自己的日志（滤掉 SDK 噪音）:"
    echo "  grep -v '^\\[Lark\\]\\|^INFO:Lark' $LOG_DIR/lark-stderr.log | tail -20"
}

cmd_uninstall() {
    stop_service
    if [ "$OS" = "macos" ]; then
        rm -f "$PLIST"
    else
        systemctl --user disable herdr-lark.service 2>/dev/null || true
        rm -f "$UNIT"
        systemctl --user daemon-reload 2>/dev/null || true
    fi
    echo "已卸载 ${LABEL}。"
}

cmd_restart() {
    load_config
    is_installed || { echo "未安装。先运行 $0 安装。"; exit 1; }
    stop_service
    sleep 1
    start_service
    echo "已重启 ${LABEL}。"
}

cmd_install() {
    load_config

    # --- 前置检查：缺什么直接说清楚，不要装完才发现起不来 ---

    [ -f "$SECRETS_FILE" ] || {
        echo "缺少 $SECRETS_FILE。"
        echo "先跑 ./install-service.sh 生成基础配置。"
        exit 1
    }

    MISSING=""
    [ -n "${HERDR_LARK_APP_ID:-}" ]     || MISSING="$MISSING HERDR_LARK_APP_ID"
    [ -n "${HERDR_LARK_APP_SECRET:-}" ] || MISSING="$MISSING HERDR_LARK_APP_SECRET"
    if [ -n "$MISSING" ]; then
        echo "缺少飞书凭据:$MISSING"
        echo ""
        echo "在 $SECRETS_FILE 里补上（权限 0600）:"
        echo "  HERDR_LARK_APP_ID=cli_xxxxxxxx"
        echo "  HERDR_LARK_APP_SECRET=xxxxxxxx"
        echo "  HERDR_LARK_CHAT_ID=oc_xxxxxxxx"
        echo ""
        echo "获取方式见 docs/lark-client-manual.md 第四节。"
        exit 1
    fi

    if [ -z "${HERDR_LARK_CHAT_ID:-}" ]; then
        echo "警告: 未设 HERDR_LARK_CHAT_ID —— 处于发现模式，任何会话都会被响应。"
    fi

    [ -n "$UV_PATH" ] || { echo "找不到 uv。安装: https://docs.astral.sh/uv/"; exit 1; }
    [ -f "$RELAY_DIR/herdr_lark.py" ] || {
        echo "找不到 $RELAY_DIR/herdr_lark.py"; exit 1;
    }

    # relay 若开了 token，HERDR_RELAY 必须带上 ?token=，否则一直 401 重连。
    if [ -n "${HERDR_RELAY_TOKEN:-}" ]; then
        case "${HERDR_RELAY:-}" in
            *token=*) : ;;
            *)
                echo "警告: relay 启用了 token，但 HERDR_RELAY 未带 ?token="
                echo "      服务会一直 401 重连。请在 $SECRETS_FILE 里改成:"
                echo "      HERDR_RELAY=ws://127.0.0.1:\${HERDR_RELAY_PORT:-8375}?token=\$HERDR_RELAY_TOKEN"
                ;;
        esac
    fi

    # 前台进程还开着的话，两个实例会抢同一条飞书长连接（集群模式随机投递）。
    RUNNING="$(pgrep -f 'herdr_lark\.py' 2>/dev/null || true)"
    if [ -n "$RUNNING" ]; then
        echo "检测到已在运行的 herdr_lark.py: $(printf '%s' "$RUNNING" | tr '\n' ' ')"
        echo "先停掉它再装服务，否则两个实例会抢同一条长连接。"
        echo "  pkill -f herdr_lark.py"
        exit 1
    fi

    mkdir -p "$LOG_DIR"
    stop_service

    if [ "$OS" = "macos" ]; then
        mkdir -p "$HOME/Library/LaunchAgents"
        SERVICE_PATH="$(dirname "$UV_PATH"):/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"
        cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>-lc</string>
        <string>set -a; [ -f "\$HOME/.config/herdr-remote/config.env" ] &amp;&amp; source "\$HOME/.config/herdr-remote/config.env"; source "\$HOME/.config/herdr-remote/secrets.env"; set +a; exec "$UV_PATH" run "$RELAY_DIR/herdr_lark.py"</string>
    </array>
    <key>WorkingDirectory</key>
    <string>$RELAY_DIR</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>ThrottleInterval</key>
    <integer>5</integer>
    <key>StandardOutPath</key>
    <string>$LOG_DIR/lark-stdout.log</string>
    <key>StandardErrorPath</key>
    <string>$LOG_DIR/lark-stderr.log</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>$SERVICE_PATH</string>
    </dict>
</dict>
</plist>
EOF
    else
        mkdir -p "$UNIT_DIR"
        cat > "$UNIT" <<EOF
[Unit]
Description=herdr-remote Lark bot
After=network-online.target herdr-relay.service
Wants=network-online.target herdr-relay.service

[Service]
ExecStart=$UV_PATH run $RELAY_DIR/herdr_lark.py
WorkingDirectory=$RELAY_DIR
Restart=always
RestartSec=5
EnvironmentFile=-$CONFIG_FILE
EnvironmentFile=$SECRETS_FILE
StandardOutput=append:$LOG_DIR/lark-stdout.log
StandardError=append:$LOG_DIR/lark-stderr.log

[Install]
WantedBy=default.target
EOF
    fi

    start_service

    echo "已安装 ${LABEL}（开机自启 + 崩溃自动重启）。"
    echo ""
    echo "  日志:   $LOG_DIR/lark-stderr.log  (Python logging 走 stderr)"
    echo "  状态:   $0 status"
    echo "  重启:   $0 restart"
    echo "  卸载:   $0 uninstall"
    echo ""
    echo "等几秒后检查是否就绪:"
    echo "  grep -v '^\\[Lark\\]\\|^INFO:Lark' $LOG_DIR/lark-stderr.log"
    echo "应看到 Bot ready / long connection thread started / Connected to relay 三条。"
}

case "${1:-install}" in
    install)   cmd_install ;;
    status)    cmd_status ;;
    restart)   cmd_restart ;;
    uninstall) cmd_uninstall ;;
    *) echo "用法: $0 [install|status|restart|uninstall]"; exit 1 ;;
esac
