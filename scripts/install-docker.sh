#!/usr/bin/env bash
# Install the published image without touching application data or browser profiles.
set -Eeuo pipefail

duo_stage='准备'
duo_child_pid=''
duo_download_temp=''
duo_config_temps=()
duo_install_dir="${DUO_INSTALL_DIR:-$HOME/applestore_shot}"
duo_download_base='https://raw.githubusercontent.com/fantasy194/applestore_shot/main'

duo_message() {
  printf '\n[%s] %s\n' "$(date '+%H:%M:%S')" "$*"
}

duo_cleanup() {
  local duo_temp
  for duo_temp in "${duo_config_temps[@]}"; do
    rm -f -- "$duo_temp"
  done
  if [[ -n "$duo_child_pid" ]]; then
    kill -TERM "$duo_child_pid" 2>/dev/null || true
    wait "$duo_child_pid" 2>/dev/null || true
  fi
  if [[ -n "$duo_download_temp" ]]; then
    rm -f -- "$duo_download_temp"
  fi
}

duo_failure() {
  local duo_code=$1
  local duo_line=$2
  printf '\n失败：%s（退出码 %s，第 %s 行）。\n' "$duo_stage" "$duo_code" "$duo_line" >&2
  if [[ "$duo_code" == 124 || "$duo_code" == 137 ]]; then
    printf '该步骤已超时；可重新运行，已有配置及 /data 数据卷保留。\n' >&2
  fi
  exit "$duo_code"
}

trap duo_cleanup EXIT
trap 'printf "\n已中断：%s。可以重新运行。\n" "$duo_stage" >&2; exit 130' INT
trap 'exit 143' TERM
trap 'duo_failure "$?" "$LINENO"' ERR

# Keep printing during registry requests and layer extraction, even when Docker
# has not produced a new progress event. timeout owns only this command's group.
duo_run() {
  "$@" &
  duo_child_pid=$!
  local duo_started=$SECONDS
  local duo_next_progress=5
  while kill -0 "$duo_child_pid" 2>/dev/null; do
    sleep 0.2
    local duo_elapsed=$((SECONDS - duo_started))
    if (( duo_elapsed >= duo_next_progress )) && kill -0 "$duo_child_pid" 2>/dev/null; then
      duo_message "$duo_stage：仍在执行，已等待 ${duo_elapsed} 秒…"
      duo_next_progress=$((duo_elapsed + 5))
    fi
  done
  local duo_code=0
  wait "$duo_child_pid" || duo_code=$?
  duo_child_pid=''
  return "$duo_code"
}

duo_download() {
  local duo_relative=$1
  if [[ -s "$duo_relative" ]]; then
    duo_message "保留已有文件：$duo_relative"
    return
  fi
  duo_message "下载 $duo_relative（连接最多 10 秒，每次下载最多 60 秒，失败重试 1 次）"
  duo_download_temp=$(mktemp "${duo_relative}.download.XXXXXX")
  duo_run timeout --kill-after=5s 130s curl \
    --fail --location --show-error --progress-bar \
    --connect-timeout 10 --max-time 60 --retry 1 \
    "$duo_download_base/$duo_relative" --output "$duo_download_temp"
  if [[ ! -s "$duo_download_temp" ]]; then
    printf '下载结果为空：%s\n' "$duo_relative" >&2
    return 1
  fi
  mv -- "$duo_download_temp" "$duo_relative"
  duo_download_temp=''
}

# Update only a byte-identical official configuration or the last installed
# version recorded locally. Local edits are never overwritten by the installer.
duo_config_candidate() {
  local duo_relative=$1
  local duo_legacy_hash=$2
  local duo_current_hash=''
  local duo_saved_hash=''
  if [[ -s "$duo_relative" ]]; then
    duo_current_hash=$(sha256sum "$duo_relative")
    duo_current_hash=${duo_current_hash%% *}
    if [[ -s "${duo_relative}.duo-sha256" ]]; then
      read -r duo_saved_hash < "${duo_relative}.duo-sha256"
    fi
    if [[ "$duo_current_hash" != "$duo_legacy_hash" && "$duo_current_hash" != "$duo_saved_hash" ]]; then
      duo_message "保留已有自定义配置：$duo_relative；如需浏览器容器，请按 Docker 文档手动合并。"
      duo_candidate=$duo_relative
      return
    fi
  fi
  duo_candidate=$(mktemp "${duo_relative}.download.XXXXXX")
  duo_config_temps+=("$duo_candidate")
  duo_message "下载并校验项目托管配置：$duo_relative"
  duo_run timeout --kill-after=5s 130s curl \
    --fail --location --show-error --progress-bar \
    --connect-timeout 10 --max-time 60 --retry 1 \
    "$duo_download_base/$duo_relative" --output "$duo_candidate"
  [[ -s "$duo_candidate" ]]
}

duo_config_commit() {
  local duo_relative=$1
  local duo_source=$2
  if [[ "$duo_source" == "$duo_relative" ]]; then
    return
  fi
  if [[ -s "$duo_relative" ]]; then
    cp -p -- "$duo_relative" "${duo_relative}.backup.$(date '+%Y%m%d%H%M%S').$$"
  fi
  mv -- "$duo_source" "$duo_relative"
  local duo_hash
  duo_hash=$(sha256sum "$duo_relative")
  printf '%s\n' "${duo_hash%% *}" > "${duo_relative}.duo-sha256"
}

duo_stage='1/6 检查系统和 Docker'
duo_message "$duo_stage"
for duo_command in docker curl timeout mktemp sha256sum; do
  if ! command -v "$duo_command" >/dev/null 2>&1; then
    printf '缺少命令：%s。请先安装 Docker Compose、curl 及 coreutils。\n' "$duo_command" >&2
    exit 1
  fi
done
case "$(uname -m)" in
  aarch64|arm64|x86_64) ;;
  *) printf '需要 64 位 ARM64 或 x86-64 系统。\n' >&2; exit 1 ;;
esac
duo_run timeout --kill-after=5s 20s docker compose version
duo_run timeout --kill-after=5s 20s docker info --format '{{.OSType}}/{{.Architecture}}'

mkdir -p -- "$duo_install_dir/docker"
cd -- "$duo_install_dir"
duo_message "部署目录：$PWD；沿用已有 Compose 项目和数据卷。"

duo_stage='2/6 下载配置文件'
duo_message "$duo_stage"
duo_config_candidate compose.yaml 54ad9625e35ed9dcdc8170a6f81a8aef3a2e44d06b20d2f84e9576fd0b75cf2d
duo_compose_input=$duo_candidate
duo_download docker/chrome-seccomp.json

duo_stage='3/6 配置自动更新并检查 Compose'
duo_message "$duo_stage"
duo_config_candidate compose.autoupdate.yaml 58820b5d79f5f382564529460af92566bd3ec35d5c9469c75d573c77e3b0d986
duo_overlay_input=$duo_candidate

duo_compose=(docker compose --progress plain -f "$duo_compose_input" -f "$duo_overlay_input")
duo_run timeout --kill-after=5s 20s "${duo_compose[@]}" config --quiet
duo_config_commit compose.yaml "$duo_compose_input"
duo_config_commit compose.autoupdate.yaml "$duo_overlay_input"
duo_compose=(docker compose --progress plain -f compose.yaml -f compose.autoupdate.yaml)

duo_stage='4/6 拉取应用、浏览器及 Watchtower 镜像（最多 20 分钟）'
duo_message "$duo_stage"
duo_run timeout --kill-after=10s 20m "${duo_compose[@]}" pull

duo_stage='5/6 启动服务并等待健康检查（最多 180 秒）'
duo_message "$duo_stage"
duo_run timeout --kill-after=10s 240s "${duo_compose[@]}" \
  up --detach --no-build --pull never --wait --wait-timeout 180

duo_stage='6/6 显示运行状态和首次访问密码'
duo_message "$duo_stage"
duo_run timeout --kill-after=5s 20s "${duo_compose[@]}" ps
duo_run timeout --kill-after=5s 20s "${duo_compose[@]}" logs --tail 30 app
duo_message '完成。自动更新按 compose.autoupdate.yaml 配置执行，首次配置默认为每 5 分钟检查；原 /data 数据卷保留。'
duo_message '默认本机入口：http://127.0.0.1:17654（自定义端口以以上状态为准）；其他电脑可使用 SSH 端口转发。'
