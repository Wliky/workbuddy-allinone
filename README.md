# WorkBuddy 一体化控制台

把 **上游网关 + Web 管理面板 + OpenAI 兼容入口** 收进**一个容器、一个端口**。

镜像构建三个架构：`linux/amd64`、`linux/arm64`、`linux/arm/v7`。
armv7（32 位 ARM）是明确支持的目标，依赖选型与前端方案都按它能跑通来定。

```bash
docker compose up -d      # 然后打开 http://<设备IP>:7864
```

---

## 一、这是什么，以及它和「两个容器拼起来」有什么不同

上游 [`Sliverkiss/workbuddy2api`](https://github.com/Sliverkiss/workbuddy2api) 是一个 Go 写的
CodeBuddy → OpenAI 兼容反向网关，只有命令行。
社区面板 [`ithtelab/workbuddy-manager`](https://github.com/ithtelab/workbuddy-manager)
给它补了一套 Next.js + FastAPI 的 Web 控制台，但把两者作为**两个独立容器**部署。

本项目走另一条路：**拉上游源码、把网关二进制编进自己的镜像，面板与网关同进程组、同端口**。

```
                    ┌──────────────── 一个容器 · 一个端口 ────────────────┐
浏览器 ──7864──────▶│  FastAPI（本项目的 app/）                           │
OpenAI 客户端 ──/v1─▶│   ├─ /            面板前端（零构建静态页）           │
                    │   ├─ /api/*       面板 API（登录/账号/密钥/日志/设置）│
                    │   └─ /v1/*        OpenAI 兼容网关（自研密钥+审计+配额）│
                    │                                                   │
                    │  supervisor ── fork ──▶ wb2api（上游 Go 二进制）    │
                    │                          └─ 监听 127.0.0.1:7863   │
                    │                             （容器回环，外部不可达） │
                    └───────────────────────────────────────────────────┘
                                        │
                                        ▼
                              腾讯 CodeBuddy（OAuth 凭证在 auths/）
```

### 换来换去，取舍在哪

| | 两个容器（上游官方 + 社区面板） | 本项目（一体化） |
|---|---|---|
| 镜像里的活动部件 | docker CLI + compose 插件 + docker.sock | 无（进程由面板直接托管） |
| armv7 前端构建 | 需要 Node 构建 Next.js，armv7 上很痛 | 不需要（静态页，零构建） |
| 上游重启 | 容器编排层 `docker compose up --build` | 一次 `fork`/`kill` |
| 权限模型 | 面板挂 docker.sock ≈ 宿主 root | 只读自己那两个卷 |
| 上游/面板独立升级 | 各自独立 | 一起升（**代价就在这里**） |
| 镜像体积 | 两套运行时 | 一套 |

**唯一真实的代价**：上游和面板同生共死，不能单独升降某一个。
考虑到面板本来就是给上游配的、两者接口强耦合，这个代价我认为值得 —— 换来的是
设备上少一层权限暴露、少一类 Docker 版本兼容问题、少一个会失配的编排层。

### 三个明确的工程决定

1. **不引入 uvicorn[standard]**。`uvloop` / `httptools` / `PyYAML` 在 PyPI 上
   **没有任何 armv7 wheel**（实测 0 个），pip 会退到源码编译，而 slim 镜像没有 gcc →
   构建必挂。裸 uvicorn（asyncio + h11）对这个量级的面板完全够用。
2. **前端不用框架、不用打包器**。armv7 上少一层构建就少一类坑，页面加载也更快。
3. **Go 阶段原生交叉编译而不是 QEMU**。`CGO_ENABLED=0` 时 Go 自己就是交叉编译器，
   钉在 `--platform=$BUILDPLATFORM` 上用 amd64 runner 全速跑，比在模拟的 armv7 里编译快一个数量级。

---

## 二、目录结构

```
workbuddy-allinone/
├── app/                     Python 后端（本项目的核心，全部自研）
│   ├── main.py              入口：装配路由、启动上游、静态托管
│   ├── settings.py          环境变量 → 常量
│   ├── security.py          PBKDF2 口令、HMAC 会话、密钥散列、IP/CIDR
│   ├── db.py                SQLite（管理员/密钥/日志/审计/元数据）
│   ├── supervisor.py        ★ 上游进程托管（替代 docker CLI + compose）
│   ├── upstream.py          上游客户端 + config.json 读写 + 凭证落盘
│   ├── oauth.py             ★ 扫码加号（设备授权，进程内纯 HTTP）
│   └── routers/             auth / accounts / keys / logs / settings_api / gateway
├── web/                     前端（零构建，index.html + style.css + app.js）
├── upstream-src/            构建时拉下来的上游源码（不进版本库）
├── upstream.lock            上游仓库与 ref 的锁定声明
├── scripts/
│   └── fetch-upstream.sh    按 upstream.lock 拉上游源码
├── tests/
│   └── smoke_test.py        端到端（真起服务、真发 HTTP，75 项断言）
├── Dockerfile               两阶段：Go 交叉编译 → python:3.12-slim 运行时
├── docker-compose.yml
└── .github/workflows/build.yml
```

---

## 三、快速开始

### 3.1 本地构建

```bash
bash scripts/fetch-upstream.sh          # 拉上游源码到 upstream-src/
docker build -t workbuddy-allinone:local .
# 国内构建建议带镜像源：
# docker build --build-arg DEBIAN_MIRROR=mirrors.aliyun.com \
#              --build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple \
#              -t workbuddy-allinone:local .
```

### 3.2 用 CI 构建好的多架构镜像

推到 GitHub 后，`.github/workflows/build.yml` 会构建
`linux/amd64, linux/arm64, linux/arm/v7` 三个平台并推到 GHCR，
额外产出一份 armv7 离线 tar.gz（国内拉不动 ghcr.io 时用）。

**记得把 GHCR 包设为 public**，否则设备要登录才能拉。

### 3.3 部署

```bash
mkdir -p data upstream/auths upstream/data
cp <上游仓库>/config.example.json upstream/config.json   # 可选，不准备也行
chown -R 10001:10001 data upstream                      # Linux 宿主必须做
$EDITOR docker-compose.yml                              # 改 WB_ADMIN_PASSWORD
docker compose up -d
docker compose logs workbuddy | grep 随机                # 没设密码时在这里看初始密码
```

打开 `http://<设备IP>:7864`。

> **`chown -R 10001:10001` 为什么必须做**：容器内进程以 uid 10001 运行，
> 而 bind mount 的目录属主由宿主决定，镜像里的 chown 不起作用。
> 属主不对的典型症状是「面板显示 0 个账号、auths/ 里明明有文件」，
> 或者启动时报 `sqlite3.OperationalError: unable to open database file`。

## 四、armv7 设备注意事项

| 项 | 说明 |
|---|---|
| 架构确认 | 先 `uname -m`。输出 `armv7l` 用 armv7 镜像；`aarch64` 用 arm64 镜像 |
| 内存 | 双运行时（Python 面板 + Go 网关）约 400–500MB。1GB 内存能跑但不宽裕，512MB 不建议 |
| 磁盘 | 镜像约 400–500MB（比双容器方案少掉 docker CLI + compose 插件 + Node 产物），加数据卷后小容量存储也够用 |
| ghcr.io | 国内经常超时。用 CI 产出的 armv7 离线包：`docker load -i workbuddy-armv7.tar.gz`，再把 compose 里的 image 改成 `workbuddy-allinone:armv7` |
| GOARM | `GOARM=7` 要求 ARMv7-A + VFPv3 以上，Cortex-A5/A7/A9 这类常见 armv7 核心都满足 |
| 老内核 / 老 Docker | 镜像构建时已关掉 `provenance`/`sbom`，避免老客户端遇到 manifest 里的 attestation 条目报 `unknown/unknown platform` |
| 首次启动 | 首次会自动生成 `upstream/config.json` 并写入一把随机内部 API 密钥。上游只监听容器回环，这把密钥是双保险 |

---

## 五、更新流程（这是「源码不受影响」的实现方式）

```
上游发新版
   ↓  ① 去 fork 页面 Sync fork（GitHub 的 fork 不会自动跟随官方仓库）
   ↓  ② 需要钉版本就改 upstream.lock 的 ref；跟随最新则保持 master
   ↓  ③ CI 重新从 upstream.lock 的仓库拉源码 → 交叉编译 → 出新镜像
   ↓  ④ docker compose pull && docker compose up -d
```

上游源码默认从 **自己的 fork** 拉（`upstream.lock` 里 `repo=Wliky/workbuddy2api`）。
想在 fork 里放自己的补丁尽管放 —— 下面这段说明对它同样成立。

> **⚠️ 别忘了第 ① 步**：`ref=master` 跟的是**你 fork 的 master**，不是官方仓库的 master。
> 没 Sync 的话，就算官方已经发了很多新版，构建出来的还是旧代码，而且不会有任何报错提示。
> 想省掉这一步就把 `repo` 改回 `Sliverkiss/workbuddy2api`（代价是没法在源码层加自己的改动）。

**你的定制为什么不会被覆盖**：面板不对上游源码做任何 `git` 操作。
上游代码只在**构建期**被拉进 `upstream-src/`、编成二进制后 COPY 进镜像；
运行期容器里根本没有上游源码树，也就没有「git pull/reset 把本地改动抹掉」这条路径。

对比之下，社区面板的「一键更新上游」是在运行目录里 `git fetch/pull` + `git checkout -- .`，
所以它必须靠「检测并保留 compose 定制」这种补丁式机制来保护用户改动 —— 一体化方案从结构上就没有这个问题。

**代价**：升级粒度是「一起升」。如果你确实需要单独升降某一个，把
`WB_UPSTREAM_EXTERNAL=1` 打开、`WB_UPSTREAM_ADDR` 指向外部上游即可退回
「面板 + 外部上游」模式（此时上游进程不由本服务托管，启动/停止/重启按钮会如实提示不可用）。

---

## 六、扫码加号（设备授权登录）

加号走 **OAuth 设备授权**，三步 HTTP 直接跑在面板进程里：

```
1. POST {base}/v2/plugin/auth/state?platform=CLI   → {state, authUrl}
2. 用户在浏览器/手机里完成授权
3. GET  {base}/v2/plugin/auth/token?state=<state>  → 未完成：HTTP 200 + code≠0
                                                     完成：{accessToken, refreshToken, expiresIn, domain}
   GET  {base}/v2/plugin/login/account?state=<state>（带 Bearer）→ {uid, enterpriseId, nickname}
```

`app/oauth.py` 的端点、请求头、信封语义逐字对齐上游 `cmd/login/main.go` 的实测实现，
没有自行推断的部分。会话以上游签发的 `state` 为键，带来三个实际好处：

- **不依赖 `login` 二进制** —— 镜像里少一个必须内置的产物，也少一处 fork 失败面。
- **轮询在面板里做** —— 前端每 3 秒自动检测授权完成，不需要用户手点「我已登录」。
- **同一 realm 可以并发开多个加号**；未完成会话持久化到 DB，面板重启后不必重新取链接。

### 收尾动作按 realm 分叉（与上游 `login.sh` 一致）

| realm | 落盘后做什么 |
|---|---|
| `cn` | 每日签到 `POST /v2/billing/meter/daily-checkin`（幂等，重复签到只回文案） |
| `global` | 查注册状态 → 缺地区则补（未指定时取白名单首个 HK）→ 领 trial 加油包（`14051` 视为已领） |

收尾动作**全部尽力而为**：失败只体现在返回文案里，不影响「账号已登录」这个事实。
已经加过号但地区没补上的国际版账号，可在账号页点「补做注册」重跑一次（幂等）。

### 两条路可切

`WB_LOGIN_MODE=http`（**默认**）走上面的进程内实现；
`WB_LOGIN_MODE=binary` 退回调用内置 `login` 工具（与上游 CLI 行为逐字一致，作为出问题时可对照的基准）。
两条路写出**同样格式**的 auth 文件，下游完全无感 —— 二进制路径在 `app/upstream.py`。

会话 TTL 默认 15 分钟（`WB_LOGIN_TTL`）：超时后轮询会收到明确的「已过期」，
而不是无限 pending。uid 落盘前会校验字符集，`uid=../x` 这类值会被拒绝，避免写穿 `auths/`。

---

## 七、接口面

### 面板 API（需登录，Cookie 会话）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/login` `/api/logout` `/api/session/renew` `/api/password` | 认证与会话 |
| GET | `/api/me` | 当前身份 |
| GET | `/api/accounts` | 账号池状态（含凭证文件与未加载账号诊断） |
| POST | `/api/accounts/{uid}/{disable\|enable\|revive}` | 手动停用 / 恢复 / 复活 |
| DELETE | `/api/accounts/{uid}` | 删除本地凭证（随后自动重启上游） |
| POST | `/api/accounts/reload` | 手工增删凭证文件后重新加载账号池 |
| POST | `/api/accounts/login/start` | 发起加号 → `{state, url}`（不阻塞） |
| GET | `/api/accounts/login/poll?state=` | 轮询一次；未完成 `{done:false}`，完成则落盘并返回账号 |
| GET | `/api/accounts/login/pending` | 未完成的加号会话（刷新页面/重启后面板据此续上轮询） |
| GET | `/api/accounts/login/regions` | 国际版注册可选地区 |
| POST | `/api/accounts/login/cancel` | 放弃某个待授权会话 |
| POST | `/api/accounts/refresh-region` | 给已存在的国际版账号补做注册激活（幂等） |
| GET/POST/PATCH/DELETE | `/api/keys` | 密钥分发、配额、模型/IP 白名单 |
| GET/DELETE | `/api/logs` | 请求审计与清理 |
| GET | `/api/stats/summary` `/api/stats/upstream` | 用量统计 |
| GET | `/api/models` | 上游实时模型列表 |
| POST | `/api/playground` `/api/playground/stream` | 聊天测试台 |
| GET/PUT | `/api/settings` | 上游 config.json 读写 |
| GET | `/api/settings/schedule-hint` | 定时任务翻译成人话 |
| GET | `/api/system/health` `/api/system/about` `/api/system/audit` | 运行状态 |
| GET | `/api/system/upstream/logs` | 上游进程日志（内存缓冲 / 文件） |
| POST | `/api/system/upstream/{start\|stop\|restart}` | 上游进程控制 |

### 对外网关（用面板分发的密钥）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/v1/chat/completions` | 流式 / 非流式，原样字节透传 |
| GET | `/v1/models` | 按密钥的模型白名单过滤 |
| GET | `/api/healthz` `/healthz` | 无鉴权，供容器探针与反代使用 |

客户端接入：

```bash
export OPENAI_BASE_URL=http://<设备IP>:7864/v1
export OPENAI_API_KEY=wbk-xxxx        # 在面板「API 密钥」里生成，明文只显示一次
```

---

## 八、安全设计

- **密钥只存 SHA-256 散列**，明文仅在创建响应里出现一次，任何接口都取不回。
- **面板口令用 PBKDF2-SHA256**（默认 60000 轮，可用 `WB_PBKDF2_ITERS` 调整；
  A5 这类弱 CPU 上轮数越高登录越慢，但每次登录只算一次）。
- **会话 Cookie 是 HMAC-SHA256 签名的**，密钥持久化在数据卷，重启不掉线；
  空闲超窗口自动失效，活跃时前端静默续签。
- **同 IP 连续登录失败 8 次锁定 15 分钟**。
- **网关侧三道闸**：密钥状态（停用/过期/配额）、模型白名单、入站 IP/CIDR 白名单。
- **不挂 docker.sock**：面板没有任何宿主权限，重启上游只是 kill 自己的子进程。
- **路径穿越防护**：静态兜底路由解析后强制校验仍在 `web/` 之内。
- **API 文档默认关闭**（`WB_ENABLE_DOCS=1` 打开 `/api/docs`）。
- 管理端默认建议只绑 `127.0.0.1` 并挂反代；要局域网直连就务必强密码 + 防火墙。

---

## 九、环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `WB_MANAGER_PORT` | `7864` | 唯一对外端口 |
| `WB_ADMIN_USERNAME` / `WB_ADMIN_PASSWORD` | `admin` / 随机 | 首启创建管理员，仅在无用户时生效 |
| `WB_SESSION_DAYS` / `WB_SESSION_IDLE_HOURS` | `7` / `24` | 会话总时长 / 空闲窗口 |
| `WB_TRUST_PROXY` | `0` | 反代后置 `1`，真实 IP 取 `X-Real-IP` |
| `WB_SECURE_COOKIE` | `auto` | `auto` / `1` / `0` |
| `WB_UPSTREAM_ADDR` | `127.0.0.1:7863` | 上游监听地址 |
| `WB_UPSTREAM_EXTERNAL` | `0` | `1` = 上游不在本容器内托管 |
| `WB_AUTO_START_UPSTREAM` | `1` | 启动时是否拉起上游 |
| `WB_UPSTREAM_TIMEOUT` | `300` | 单次上游请求超时（秒） |
| `WB_LOG_RETENTION_DAYS` / `WB_LOG_KEEP_MAX` | `14` / `50000` | 请求日志保留策略 |
| `WB_UPSTREAM_LOG_LINES` | `800` | 内存里保留的上游日志行数 |
| `WB_LOGIN_MODE` | `http` | 加号方式：`http` 进程内设备授权 / `binary` 调内置 `login` 工具 |
| `WB_OAUTH_CN_BASE` / `WB_OAUTH_CN_ORIGIN` | `https://copilot.tencent.com` / `https://www.codebuddy.cn` | 国内版授权端点与 Origin 头 |
| `WB_OAUTH_GLOBAL_BASE` / `WB_OAUTH_GLOBAL_ORIGIN` | `https://www.workbuddy.ai` | 国际版授权端点与 Origin 头 |
| `WB_LOGIN_TTL` | `900` | 待授权会话存活秒数 |
| `WB_OAUTH_TIMEOUT` | `30` | 单次授权请求超时（秒） |
| `WB_OAUTH_COOKIE_JAR` | `0` | 授权请求是否复用 cookie jar（默认不复用，只靠 state 承载会话） |
| `WB_PBKDF2_ITERS` | `60000` | 口令散列轮数 |
| `WB_DATA_DIR` / `WB_UPSTREAM_DIR` / `WB_STATIC_DIR` / `WB_BIN_DIR` | `/srv/...` | 路径覆盖 |

---

## 十、验证状态

项目自带一套端到端测试（`tests/smoke_test.py`，75 项断言）。它**不 mock 本项目代码**：
起真的 HTTP 假服务 —— 一个假上游网关（`/healthz` `/status` `/v1/models`
`/v1/chat/completions` `/admin/accounts/*` `/v1/stats`）+ 两个假设备授权端（cn / global），
真的起 uvicorn，再用真 HTTP 请求把面板、网关与加号流程打一遍。

假授权端刻意复刻了真实上游的两个坑点，否则这段测试等于没测：

- `auth/token` 在用户授权前返回 **HTTP 200 + code≠0**（不是 4xx）；
- 地区列表的 `data` 是**被序列化成字符串的 JSON**（双层信封）。

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r app/requirements.txt
python tests/smoke_test.py                  # 用当前解释器跑被测服务
python tests/smoke_test.py --python /path/to/venv/bin/python
```

已验证通过的包括：

- 登录/鉴权边界（错误口令 401、未登录访问管理接口 401、会话续签）
- 账号池状态归一（多重状态时主导原因正确、被折叠的事实不丢、凭证文件与账号关联）
- 账号停用/启用/复活转发到上游 admin 接口
- **config.json 写入语义**：只覆盖提交的键；打码占位符 `__SET__` 不会被当成新密钥写回
- 密钥创建返回明文、列表不含明文
- 网关：无密钥/错密钥 401、模型白名单 403、IP 白名单 403、`/v1/models` 按白名单过滤
- **流式记账**：从字节流尾部缓冲里提取最后一条 `usage`，`prompt_tokens` /
  `completion_tokens` / 扣费都正确落库（这条最容易出错，专门断言了）
- **扫码加号全链路**：取链接 → 未授权时轮询 `done:false` 并透传上游文案 → 授权后落盘，
  凭证的 `account/auth` 嵌套结构、`realm`、`expiresAt`（由 `expiresIn` 换算）逐项核对
- **加号收尾动作真的打到了端点**：CN 每日签到被调用一次；国际版走完
  「查注册 → 取地区（双层信封）→ 补地区 → 领 trial」，且未指定地区时取白名单首个
  **HK 而非接口返回顺序**（接口故意把 SG 排在前面）
- 加号会话：同一 realm 可并发开两个、取消后从待授权列表消失、未知 `state` 返回 404
- 路径穿越防护（`/%2e%2e/app/main.py` 不会回传后端源码）、未知前端路由回落 index.html
- 全程无 Traceback / ERROR

**未验证 / 需要你在真机上确认的**：

1. **上游 Go 子进程的真实生命周期**（fork / 崩溃退避重启 / SIGTERM 整组收掉）
   在 Windows 上无法验证，测试走的是 `WB_UPSTREAM_EXTERNAL=1` 分支。
   在 Linux 容器里首次部署时请确认「系统设置 → 上游进程日志」有正常输出。
2. **加号流程没有真实 CodeBuddy 账号跑过**。协议的每一步都已对着假授权端跑通
   （含端点被真实调用的次数），但真实账号的授权页跳转、扫码形态、以及国际版注册
   接口的实际返回码仍需真机确认。首次使用时建议先加一个 cn 账号观察返回文案。
3. **armv7 实机**：镜像在 GitHub Actions 上构建，未在真实 armv7 设备上验证。

移植依据的实测数据（基础镜像架构声明、PyPI 轮子统计、跨架构构建注意事项）见
[`docs/armv7-findings.md`](docs/armv7-findings.md)。

---

## 十一、许可与致谢

- 本项目代码：MIT。
- 上游网关 [`Sliverkiss/workbuddy2api`](https://github.com/Sliverkiss/workbuddy2api)（MIT）：
  本项目在构建期拉取其源码、编译其二进制；`app/upstream.py` 里的 OAuth 两步式流程
  （`login url` → `login poll`）、auth 文件字段、CN 签到端点与请求头、
  `/status` 与 `/admin/accounts/*` 的接口形状，均依据其源码与 `login.sh` 的实测实现对齐。
- 加号的进程内实现参考了
  [`linguo2625469/workbuddy2api-panel`](https://github.com/linguo2625469/workbuddy2api-panel)：
  它把设备授权三步做成了**进程内纯 HTTP**（不依赖 `login` 二进制），本项目据此确认了
  `auth/state` / `auth/token` / `login/account` 三段的端点与信封语义、以及国际版
  「注册激活 + trial」的收尾顺序与 `14051` 幂等码的处理。代码为本项目自行实现。
- 面板的信息架构参考了 [`ithtelab/workbuddy-manager`](https://github.com/ithtelab/workbuddy-manager)（MIT）：
  账号双状态位（`disabled` 系统判定 vs `manual_disabled` 运维主动）、
  密钥分发/审计/用量统计的运营面、以及「扫码加号 → 自动签到 → 重载上游」的流程顺序。
  前端与后端代码均为本项目自行实现，未复用其代码。
- 上游 CodeBuddy 是第三方商业产品。用其账号做 API 网关涉及目标平台服务条款与账号风险，
  请自行评估；本项目不对账号封禁、条款违约或使用结果负责。
