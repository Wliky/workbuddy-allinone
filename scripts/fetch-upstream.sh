#!/usr/bin/env bash
# 把上游源码拉到 upstream-src/，供 Dockerfile 编译。
#
# 用法：
#   bash scripts/fetch-upstream.sh                 # 按 upstream.lock 拉取
#   bash scripts/fetch-upstream.sh <repo> <ref>    # 临时覆盖
#
# 为什么要把上游源码放到构建上下文里、而不是在 CI 里先编译好再 COPY：
#   · Dockerfile 自成一体，本地 `docker build` 与 CI 走完全相同的路径；
#   · 交叉编译在 gobuild 阶段内完成，不需要 runner 装 Go；
#   · 升级上游 = 改 upstream.lock 里的一行，不需要动任何构建逻辑。
set -euo pipefail

cd "$(dirname "$0")/.."

REPO=""
REF=""
if [ $# -ge 2 ]; then
    REPO="$1"; REF="$2"
else
    LOCK="upstream.lock"
    [ -f "$LOCK" ] || { echo "缺少 $LOCK" >&2; exit 1; }
    while IFS='=' read -r k v; do
        case "$(printf '%s' "$k" | tr -d '[:space:]')" in
            repo) REPO="$(printf '%s' "$v" | tr -d '[:space:]')" ;;
            ref)  REF="$(printf '%s' "$v" | tr -d '[:space:]')" ;;
            ''|\#*) ;;
        esac
    done < "$LOCK"
fi
[ -n "$REPO" ] || { echo "upstream.lock 里没有 repo=" >&2; exit 1; }
[ -n "$REF" ] || REF=master

DEST="upstream-src"
rm -rf "$DEST"
mkdir -p "$DEST"

echo "拉取上游 https://github.com/${REPO}.git  ref=${REF}"
# 第一次失败的原因必须打出来：最常见的是仓库改名/删除/转私有，
# 表现为 git 反问 "Username for 'https://github.com'"，只看退出码完全看不出是这回事。
if ERR="$(git clone --depth 1 --branch "$REF" "https://github.com/${REPO}.git" "$DEST" 2>&1)"; then
    :
else
    # ref 可能是 commit（--branch 只认分支/标签），退化为完整克隆后 checkout
    echo "按 ref 直接克隆失败：${ERR}" >&2
    echo "改为完整克隆后再检出…" >&2
    rm -rf "$DEST"; mkdir -p "$DEST"
    if ! ERR2="$(git clone "https://github.com/${REPO}.git" "$DEST" 2>&1)"; then
        echo "完整克隆也失败了：${ERR2}" >&2
        echo "" >&2
        echo "拉不到 ${REPO}。请确认：仓库存在、名字没拼错、且是 public" >&2
        echo "（私有仓库需要改用 SSH 或带 token 的地址）。upstream.lock 里的" >&2
        echo "repo= 与 ref= 决定这里拉的是谁。" >&2
        exit 1
    fi
    git -C "$DEST" checkout "$REF"
fi

COMMIT="$(git -C "$DEST" rev-parse HEAD)"
echo "$COMMIT" > upstream-src.commit
echo "上游提交：$COMMIT"

# 上游把 .github 里的东西也带进来了，清掉减小构建上下文
rm -rf "$DEST/.github" "$DEST/.git"

# 立刻验证关键构建输入是否齐全，避免 docker build 跑到一半才报
for f in go.mod cmd/server cmd/login cmd/signin config.example.json; do
    [ -e "$DEST/$f" ] || { echo "上游源码缺少 $f —— 上游结构变了？" >&2; exit 1; }
done
echo "上游源码就绪：$DEST"
