# armv7 移植的实测依据

这份笔记记录的是**实测数据**，不是推测。它是本项目几个关键取舍的依据来源，
也是下次要往别的架构（或在别的基础镜像上）移植时的核查清单。

## 1. 基础镜像的 armv7 支持

来源：`docker-library/official-images` 仓库的 `library/<image>` 文件里的
`Architectures:` 字段（这是 Docker 官方镜像支持哪些平台的权威声明）。

| 镜像 | arm32v7 | 备注 |
|---|---|---|
| `golang:1.26-alpine`（1.26.8-alpine3.23 / 3.24） | ✅ | amd64, arm32v6, arm32v7, arm64v8, i386, ppc64le, riscv64, s390x |
| `alpine:3.21 ~ 3.24` | ✅ | 全系列都带 arm32v7 |
| `alpine:3.20` | ⚠️ | 已从官方列表移除（EOL），tag 可能仍能拉，但没有安全更新 |
| `python:3.12-slim`（trixie / bookworm） | ✅ | amd64, arm32v7, arm64v8, i386, ppc64le |
| `node:22-bookworm-slim` | ✅ | 唯一还带 arm32v7 的 node 版本 |
| `node:24 / node:26` | ❌ | 已砍掉 arm32v7 |
| `node:20-*` | ❌ | Node 20 已 EOL，官方列表里已无该系列 |

> 本项目把前端做成零构建静态页，所以完全不需要 node 镜像 —— 上面这两条 node 结论
> 只对「沿用 Next.js 方案」的路线有意义。

## 2. Python 依赖的 armv7 轮子

来源：`https://pypi.org/pypi/<pkg>/json` 的 `urls` 中带 `armv7l` / `armhf` 的文件名统计。

| 包 | armv7 wheel | 后果 |
|---|---|---|
| `pydantic-core` | ✅ 17 个（cp310–cp313，manylinux_2_17_armv7l + musllinux） | 关键靠它，pydantic 才装得上 |
| `watchfiles` | ✅ 8 个 | |
| `websockets` | ✅ 14 个 | |
| `uvloop` | ❌ **0 个** | |
| `httptools` | ❌ **0 个** | |
| `PyYAML` | ❌ **0 个** | |
| `uvicorn` / `fastapi` / `httpx` / `pydantic` | ✅ 纯 Python 轮子（py3-none-any） | 本体没问题 |

**结论**：`uvicorn[standard]`（会拉进 uvloop + httptools + PyYAML）在 armv7 上**必然构建失败** ——
pip 找不到轮子会退到源码编译，而 `python:3.12-slim` 里没有 gcc，
报 `command 'gcc' failed` 就结束了。裸 `uvicorn` 用 asyncio + h11，对这个量级够用。

## 3. 工具二进制的 armv7 可得性

| 组件 | armv7 | 来源 |
|---|---|---|
| `docker-compose-linux-armv7` | ✅ | docker/compose v2.40.3 的 release assets 里确认存在 |
| docker CLI 静态包 `armhf` | 未验证 | 本机网络拦掉了 download.docker.com，无法确认 |

> 本项目把 docker CLI 与 compose 插件整个去掉了（上游进程由 `app/supervisor.py` 直接托管），
> 所以这两项在本方案里不再是依赖 —— 上面的 docker CLI 不确定项自动消失了。

## 4. armv7 设备的硬件特点

以下以一款常见的 armv7 盒子（Amlogic S805：4×Cortex-A5 @1.5GHz、1GB DDR3、8GB eMMC）
作为参照，同类设备的结论基本一致：

- `GOARM=7` 需要 ARMv7 + VFPv3；Cortex-A5 是 VFPv4 且 S805 带 NEON，满足。
- Go 的 `GOARM=7` 二进制不要求编译期 NEON，运行时按 HWCAP 探测，兼容。
- 内存：Python 面板 + Go 网关约 400–500MB，1GB 能跑但不宽裕；512MB 版本不可行。
- 部署前用 `uname -m` 确认是 `armv7l`；部分改版/刷机版是 `aarch64`，那就用 arm64 镜像。

## 5. 跨架构构建的两个必做项

1. **Go 阶段钉 `--platform=$BUILDPLATFORM` 做原生交叉编译**。
   `CGO_ENABLED=0` 之下 Go 自己就是交叉编译器，用 amd64 runner 全速跑；
   靠 QEMU 模拟 armv7 跑 `go build` 会慢一个数量级，且在资源紧张的 runner 上容易抖。
   本项目 Dockerfile 里的写法：

   ```dockerfile
   FROM --platform=$BUILDPLATFORM golang:1.26-alpine AS gobuild
   ARG TARGETOS TARGETARCH TARGETVARIANT
   ...
   export GOOS="$TARGETOS" GOARCH="$TARGETARCH"
   [ -n "$GOARM_VAL" ] && export GOARM="$GOARM_VAL"   # v7 → 7
   ```

2. **buildx 必须关 `provenance` 与 `sbom`**。
   默认开启时 manifest 里会多出 attestation 条目，老设备上常见的 Docker 19/20
   拉取时报 `unknown/unknown platform` 直接失败。**这个坑在 amd64 机器上完全不会暴露**，
   只在目标设备上暴露，排查起来很费时间。

## 6. 复核清单（换架构时照这个走一遍）

1. 官方镜像 `Architectures:` 是否含目标平台 → `docker-library/official-images`
2. 每个 Python 依赖是否有目标平台 wheel → PyPI JSON API 的 `urls`
3. 需要下载的二进制/插件是否有对应架构的 release asset → GitHub releases API
4. 目标 CPU 的浮点/NEON 能力与 `GOARM` 要求是否匹配
5. buildx 是否关了 `provenance` / `sbom`
6. CI 是否顺带产出离线 tar.gz（目标设备往往拉不动 ghcr.io）
