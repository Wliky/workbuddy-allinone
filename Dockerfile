# 一体化镜像：一个容器 = 上游 Go 网关 + 面板 + OpenAI 兼容入口。
#
# 相对「两个容器拼起来」，这里刻意去掉了 docker CLI、compose 插件、docker.sock
# 和 Node 构建链 —— 越少的活动部件，在 armv7 这类老设备上越不容易出问题。

# syntax=docker/dockerfile:1

# ══════════════════════════════════════════════════════════════
# 阶段 1：编译上游 Go 网关（含 login / signin 等工具二进制）
# ══════════════════════════════════════════════════════════════
FROM --platform=$BUILDPLATFORM golang:1.26-alpine AS gobuild

# 钉在 BUILDPLATFORM 上做**原生交叉编译**，不要靠 QEMU 模拟 armv7 跑 go build：
# CGO_ENABLED=0 之下 Go 本身就是交叉编译器，用 runner 的 amd64 全速跑，
# 比 QEMU 快一个数量级，也避免模拟环境下的资源抖动。
ARG TARGETOS
ARG TARGETARCH
ARG TARGETVARIANT

WORKDIR /src
COPY upstream-src/go.mod ./
RUN go mod download
COPY upstream-src/ ./

RUN set -eux; \
    GOARM_VAL="$(printf '%s' "${TARGETVARIANT:-}" | sed 's/^v//')"; \
    export CGO_ENABLED=0; \
    export GOOS="${TARGETOS:-linux}"; \
    export GOARCH="${TARGETARCH:-$(go env GOARCH)}"; \
    if [ -n "$GOARM_VAL" ]; then export GOARM="$GOARM_VAL"; fi; \
    echo "构建目标：GOOS=$GOOS GOARCH=$GOARCH GOARM=${GOARM:-默认}"; \
    mkdir -p /out; \
    go build -trimpath -ldflags="-s -w" -o /out/wb2api      ./cmd/server; \
    go build -trimpath -ldflags="-s -w" -o /out/login       ./cmd/login; \
    go build -trimpath -ldflags="-s -w" -o /out/signin_bin  ./cmd/signin; \
    go build -trimpath -ldflags="-s -w" -o /out/credit      ./cmd/credit

# ══════════════════════════════════════════════════════════════
# 阶段 2：运行时
# ══════════════════════════════════════════════════════════════
# python:3.12-slim 官方同时提供 arm32v7 与 amd64/arm64，无需额外处理。
FROM python:3.12-slim

# 可选：Debian 软件源镜像（中国大陆访问官方源很慢且常 502）。
#   docker build --build-arg DEBIAN_MIRROR=mirrors.aliyun.com .
ARG DEBIAN_MIRROR=""
# 可选：PyPI 镜像。
ARG PIP_INDEX_URL=""
# 上游版本信息，仅用于界面展示（CI 传入）
ARG UPSTREAM_COMMIT=""
ARG UPSTREAM_REF="master"
# 界面上「上游仓库」的展示值，应与实际构建所用的仓库一致（即 upstream.lock 里的 repo）。
ARG UPSTREAM_REPO="Sliverkiss/workbuddy2api"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    WB_SRV_DIR=/srv \
    WB_DATA_DIR=/srv/data \
    WB_STATIC_DIR=/srv/web \
    WB_UPSTREAM_DIR=/srv/upstream \
    WB_BIN_DIR=/srv/bin \
    WB_MANAGER_PORT=7864 \
    WB_UPSTREAM_ADDR=127.0.0.1:7863 \
    WB_UPSTREAM_COMMIT=${UPSTREAM_COMMIT} \
    WB_UPSTREAM_REF=${UPSTREAM_REF} \
    WB_UPSTREAM_REPO=${UPSTREAM_REPO} \
    TZ=Asia/Shanghai

# ca-certificates：Go 二进制走 HTTPS 打上游 OAuth / 签到端点时需要系统根证书。
# curl 只用于容器内的排障，体积可忽略。
RUN set -eu; \
    set_mirror() { \
        for f in /etc/apt/sources.list /etc/apt/sources.list.d/debian.sources; do \
            [ -f "$f" ] && sed -i -E "s#(https?://)[^/ ]+#\1$1#g" "$f"; \
        done; \
    }; \
    install_pkgs() { \
        apt-get -o Acquire::Retries=1 -o Acquire::http::Timeout=20 update \
        && apt-get install -y --no-install-recommends ca-certificates curl tzdata; \
    }; \
    [ -n "${DEBIAN_MIRROR}" ] && set_mirror "${DEBIAN_MIRROR}"; \
    if ! install_pkgs; then \
        echo "apt: 当前软件源失败，改用 mirrors.aliyun.com 重试…" >&2; \
        rm -rf /var/lib/apt/lists/*; \
        set_mirror mirrors.aliyun.com; \
        install_pkgs; \
    fi; \
    rm -rf /var/lib/apt/lists/*

WORKDIR /srv

# 先装依赖，让代码改动不触发重装
COPY app/requirements.txt /srv/app/requirements.txt
RUN set -eu; \
    pip_install() { \
        pip install --no-cache-dir --retries 5 --timeout 60 --index-url "$1" \
            -r /srv/app/requirements.txt; \
    }; \
    if [ -n "${PIP_INDEX_URL}" ]; then \
        pip_install "${PIP_INDEX_URL}"; \
    elif pip_install https://pypi.org/simple; then \
        echo "pip: 官方源安装成功"; \
    else \
        echo "pip: 官方源失败，改用清华镜像重试…" >&2; \
        pip_install https://pypi.tuna.tsinghua.edu.cn/simple; \
    fi

# 我们的代码与前端（全部自研，不受上游更新影响）
COPY app /srv/app
COPY web /srv/web

# 上游二进制与示例配置
COPY --from=gobuild /out/wb2api     /srv/bin/wb2api
COPY --from=gobuild /out/login      /srv/bin/login
COPY --from=gobuild /out/signin_bin /srv/bin/signin_bin
COPY --from=gobuild /out/credit     /srv/bin/credit
COPY --from=gobuild /src/config.example.json /srv/upstream/config.example.json

# 目录与属主：面板与上游用同一个 uid，不存在「跨容器 uid 不一致导致读不到凭证」那类问题
RUN set -eux; \
    useradd -u 10001 -m -s /bin/bash app; \
    mkdir -p /srv/data /srv/upstream/auths /srv/upstream/data; \
    chmod 755 /srv/bin/*; \
    printf '%s\n' "$UPSTREAM_COMMIT" > /srv/.upstream-commit; \
    chown -R 10001:10001 /srv

USER app

# 只暴露一个端口：面板 UI + 面板 API + OpenAI 兼容网关全在这上面。
# 上游 Go 进程监听容器内回环 7863，容器外无从触及。
EXPOSE 7864

# 用 python 做探针，省掉在镜像里装 curl/wget 之外的负担
HEALTHCHECK --interval=30s --timeout=6s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; \
r=urllib.request.urlopen('http://127.0.0.1:7864/api/healthz',timeout=5); \
sys.exit(0 if r.status==200 else 1)"

CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "7864"]
