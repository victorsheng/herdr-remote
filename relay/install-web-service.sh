#!/bin/sh
# install-web-service.sh — 把运维面板装成开机自启的用户服务。
#
# 沿用 install-service.sh 的约定：配置在 ~/.config/herdr-remote/{config,secrets}.env，
# 日志在 HERDR_LOG_DIR，macOS 用 LaunchAgent、Linux 用 systemd user unit。
#
#   ./install-web-service.sh            安装并启动
#   ./install-web-service.sh status     查看状态
#   ./install-web-service.sh restart    重启
#   ./install-web-service.sh uninstall  卸载
set -eu

LABEL="com.herdr-remote.web"
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
UNIT="$UNIT_DIR/herdr-web.service"

load_config() {
    # shellcheck disable=SC1090
    [ -f "$CONFIG_FILE" ] && { set -a; . "$CONFIG_FILE"; set +a; }
    # shellcheck disable=SC1090
    [ -f "$SECRETS_FILE" ] && { set -a; . "$SECRETS_FILE"; set +a; }
    LOG_DIR="${HERDR_LOG_DIR:-$HOME/Library/Logs/herdr-remote}"
    [ "$OS" = "linux" ] && LOG_DIR="${HERDR_LOG_DIR:-$HOME/.local/state/herdr-remote}"
    UV_PATH="${HERDR_UV_PATH:-$(command -v uv || true)}"
    RELAY_DIR="${HERDR_RELAY_DIR:-$SCRIPT_DIR}"
    WEB_PORT="${HERDR_WEB_PORT:-8377}"
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
        systemctl --user stop herdr-web.service 2>/dev/null || true
    fi
}

start_service() {
    if [ "$OS" = "macos" ]; then
        launchctl bootstrap "gui/$(id -u)" "$PLIST"
    else
        systemctl --user daemon-reload
        systemctl --user enable --now herdr-web.service
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
        systemctl --user status herdr-web.service --no-pager || true
    fi
    echo ""
    echo "面板: http://127.0.0.1:$WEB_PORT"
    echo "日志: $LOG_DIR/web-stderr.log"
}

cmd_uninstall() {
    stop_service
    if [ "$OS" = "macos" ]; then
        rm -f "$PLIST"
    else
        systemctl --user disable herdr-web.service 2>/dev/null || true
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

    [ -n "$UV_PATH" ] || { echo "找不到 uv。安装: https://docs.astral.sh/uv/"; exit 1; }
    [ -f "$RELAY_DIR/herdr_web.py" ] || {
        echo "找不到 $RELAY_DIR/herdr_web.py"; exit 1;
    }

    # 面板只是聚合展示，缺凭据也能跑（对应的列显示为空），但值得提醒。
    [ -n "${HERDR_LARK_APP_ID:-}" ] \
        || echo "提示: 未设 HERDR_LARK_APP_ID，群名一列会显示 chat_id。"
    [ -n "${HERDR_LARK_OBSERVER_APP_ID:-}" ] \
        || echo "提示: 未设 HERDR_LARK_OBSERVER_APP_ID，质检覆盖一列全显示盲区。"

    RUNNING="$(pgrep -f 'herdr_web\.py' 2>/dev/null || true)"
    if [ -n "$RUNNING" ]; then
        echo "检测到已在运行的 herdr_web.py: $(printf '%s' "$RUNNING" | tr '\n' ' ')"
        echo "先停掉它再装服务，否则端口会冲突。"
        echo "  pkill -f herdr_web.py"
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
        <string>set -a; [ -f "\$HOME/.config/herdr-remote/config.env" ] &amp;&amp; source "\$HOME/.config/herdr-remote/config.env"; source "\$HOME/.config/herdr-remote/secrets.env"; set +a; exec "$UV_PATH" run "$RELAY_DIR/herdr_web.py"</string>
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
    <string>$LOG_DIR/web-stdout.log</string>
    <key>StandardErrorPath</key>
    <string>$LOG_DIR/web-stderr.log</string>
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
Description=herdr-remote web dashboard
After=network.target

[Service]
Type=simple
WorkingDirectory=$RELAY_DIR
EnvironmentFile=-$CONFIG_FILE
EnvironmentFile=$SECRETS_FILE
ExecStart=$UV_PATH run $RELAY_DIR/herdr_web.py
Restart=always
RestartSec=5
StandardOutput=append:$LOG_DIR/web-stdout.log
StandardError=append:$LOG_DIR/web-stderr.log

[Install]
WantedBy=default.target
EOF
    fi

    start_service
    echo "已安装并启动 ${LABEL}。"
    echo "面板: http://127.0.0.1:$WEB_PORT"
    echo ""
    echo "只监听回环——页面把 chat_id、pane、项目名都摊开了，不该对外网开放。"
    echo "远程看请走 Tailscale 或 SSH 端口转发:"
    echo "  ssh -N -L $WEB_PORT:127.0.0.1:$WEB_PORT $(whoami)@<这台机器>"
}

case "${1:-install}" in
    status)    cmd_status ;;
    restart)   cmd_restart ;;
    uninstall) cmd_uninstall ;;
    install|"") cmd_install ;;
    *) echo "用法: $0 [install|status|restart|uninstall]"; exit 1 ;;
esac
