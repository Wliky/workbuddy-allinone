#!/usr/bin/env python3
"""玩客云远程工具：探测环境 / 部署一体化容器。

本机没有 docker，armv7 镜像必须由 GitHub Actions 构建。这个脚本负责的是
「镜像已经有了之后」的那一段：连上玩客云、把环境摸清楚、把容器跑起来。

用法
----
    # 只探测，不改任何东西
    python scripts/remote.py probe --host 192.168.1.50 --user root --password xxx

    # 部署：从仓库拉镜像
    python scripts/remote.py deploy --host 192.168.1.50 --user root --password xxx \
        --image ghcr.io/wliky/workbuddy-allinone:armv7

    # 部署：用 CI 产出的离线 tar.gz（国内拉不动 ghcr 时走这条）
    python scripts/remote.py deploy --host 192.168.1.50 --user root --password xxx \
        --image-tar ./workbuddy-allinone-armv7.tar.gz

设计说明
--------
* 全程走 SSH，不需要在玩客云上装任何东西（除了 Docker 本身）。
* 探测是只读的；部署会创建 /srv/workbuddy 目录，但不会碰系统其他位置。
* 老旧 sshd（玩客云常见 dropbear / 老 openssh）会被 paramiko 5 以
  "no matching host key type" 拒绝，脚本会自动放宽算法重试一次。
"""
from __future__ import annotations

import argparse
import getpass
import json
import re
import secrets
import sys
import time
from pathlib import Path

PROJ = Path(__file__).resolve().parents[1]

# 部署到玩客云上的目录。1GB 内存的设备上 /tmp 可能是 tmpfs，别放那里。
REMOTE_DIR = "/srv/workbuddy"

PROBE_COMMDS = [
    ("arch", "uname -m"),
    ("kernel", "uname -r"),
    ("os", "cat /etc/os-release 2>/dev/null | head -4"),
    ("cpu", "grep -m1 -E 'model name|Hardware' /proc/cpuinfo 2>/dev/null"),
    ("cores", "nproc"),
    ("mem_mb", "free -m 2>/dev/null | awk '/^Mem:/{print $2}'"),
    ("swap_mb", "free -m 2>/dev/null | awk '/^Swap:/{print $2}'"),
    ("disk_root", "df -h / 2>/dev/null | tail -1"),
    ("docker", "docker --version 2>&1 | head -1"),
    ("docker_compose", "(docker compose version 2>&1 | head -1); (docker-compose --version 2>&1 | head -1)"),
    ("docker_running", "docker info --format '{{.ServerVersion}}' 2>&1 | head -1"),
    ("docker_storage", "docker info --format '{{.Driver}}' 2>&1 | head -1"),
    ("containers", "docker ps -a --format '{{.Names}}|{{.Status}}|{{.Image}}' 2>&1 | head -20"),
    ("images", "docker images --format '{{.Repository}}:{{.Tag}} {{.Size}}' 2>&1 | head -20"),
    ("port_7864", "(ss -ltnp 2>/dev/null || netstat -ltnp 2>/dev/null) | grep -E ':7864|:7863' || echo 'free'"),
    ("has_curl", "command -v curl || echo none"),
    ("has_bash", "command -v bash || echo none"),
    ("has_git", "command -v git || echo none"),
]


class Remote:
    """一个 SSH 连接 + 若干命令执行。"""

    def __init__(self, host, user, password=None, key_path=None, port=22):
        import paramiko

        self.host, self.user, self.port = host, user, port
        self.client = paramiko.SSHClient()
        self.client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            self.client.connect(
                hostname=host, port=port, username=user,
                password=password, key_filename=key_path,
                timeout=15, banner_timeout=20, auth_timeout=20,
                look_for_keys=False if key_path else False,
                allow_agent=bool(key_path),
            )
        except Exception as exc:  # noqa: BLE001
            msg = str(exc)
            if "no matching" in msg.lower() or "unable to agree" in msg.lower():
                # 老 sshd：放宽算法后重试
                self.client.close()
                _relax_algorithms()
                self.client = paramiko.SSHClient()
                self.client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                self.client.connect(
                    hostname=host, port=port, username=user,
                    password=password, key_filename=key_path,
                    timeout=15, banner_timeout=20, auth_timeout=20,
                    allow_agent=bool(key_path),
                )
                self.legacy = True
            else:
                raise
        else:
            self.legacy = False

    def run(self, cmd, timeout=120, quiet=False):
        _, out, err = self.client.exec_command(cmd, timeout=timeout)
        o = out.read().decode("utf-8", "replace")
        e = err.read().decode("utf-8", "replace")
        code = out.channel.recv_exit_status()
        if not quiet:
            return code, o, e
        return code, o, e

    def upload(self, local_bytes: bytes, remote_path: str, mode: int = 0o644):
        sftp = self.client.open_sftp()
        try:
            with sftp.open(remote_path, "wb") as f:
                f.write(local_bytes)
            sftp.chmod(remote_path, mode)
        finally:
            sftp.close()

    def download(self, remote_path: str) -> bytes:
        sftp = self.client.open_sftp()
        try:
            with sftp.open(remote_path, "rb") as f:
                return f.read()
        finally:
            sftp.close()

    def close(self):
        with _suppress():
            self.client.close()


def _relax_algorithms():
    """给老旧 sshd 开的口子：启用 paramiko 5 默认禁用的 sha1 / ssh-rsa。"""
    import paramiko.transport as T

    for name, extra in (
        ("_preferred_kex", ("diffie-hellman-group1-sha1",
                            "diffie-hellman-group14-sha1",
                            "diffie-hellman-group-exchange-sha1",
                            "diffie-hellman-group-exchange-sha256")),
        ("_preferred_keys", ("ssh-rsa", "ssh-dss")),
        ("_preferred_pubkeys", ("ssh-rsa", "ssh-dss")),
        ("_preferred_ciphers", ("aes128-cbc", "3des-cbc", "aes256-cbc")),
    ):
        cur = getattr(T.Transport, name, ())
        merged = tuple(dict.fromkeys(tuple(cur) + tuple(extra)))
        setattr(T.Transport, name, merged)


class _suppress:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return True


# ── 探测 ────────────────────────────────────────────────────────────
def probe(rem: Remote) -> dict:
    info = {"host": rem.host, "legacy_ssh": rem.legacy}
    for key, cmd in PROBE_COMMDS:
        code, out, err = rem.run(cmd)
        val = (out or err).strip()
        info[key] = val.splitlines()[0] if val and "\n" not in val.strip() else val
        if isinstance(info[key], str):
            info[key] = info[key].replace("\n", " | ")
        if code != 0 and key.startswith("docker"):
            info[key] = f"[exit {code}] {info[key]}"
    return info


def verdict(info: dict) -> list[tuple[str, str, str]]:
    """把探测结果翻译成「能不能部署」的结论。返回 (级别, 项, 说明)。"""
    rows: list[tuple[str, str, str]] = []
    arch = str(info.get("arch", ""))

    if arch == "armv7l":
        rows.append(("OK", "架构", "armv7l —— 与本方案目标一致，用 armv7 镜像"))
    elif arch == "aarch64" or arch == "arm64":
        rows.append(("WARN", "架构", f"{arch} —— 是 64 位！官方 arm64 镜像即可，"
                                     "别用 armv7（能跑但白白丢掉 64 位优势）"))
    elif arch == "armv6l":
        rows.append(("FAIL", "架构", "armv6l —— Go 与 Python 均不支持，无法部署"))
    else:
        rows.append(("WARN", "架构", f"{arch} —— 不是预期的 arm，需确认镜像架构"))

    mem = _int(info.get("mem_mb"))
    # 阈值按实测取：Go 网关 30–80MB + Python 面板 200–300MB + Docker 守护 100MB+。
    # 玩客云 1GB 版 free 约 900–1000MB，属于「能跑但没余量」，故 1024 以下一律提示。
    if mem and mem < 512:
        rows.append(("FAIL", "内存", f"{mem}MB —— 跑不起来，至少需要 512MB"))
    elif mem and mem < 1024:
        rows.append(("WARN", "内存", f"{mem}MB —— 能跑但没余量。建议加 swap，别再跑别的服务"))
    elif mem:
        rows.append(("OK", "内存", f"{mem}MB —— 够用"))

    dk = str(info.get("docker", ""))
    if dk.startswith("[exit") or "not found" in dk.lower():
        rows.append(("FAIL", "Docker", "未安装 —— 需先装 docker.io"))
    else:
        rows.append(("OK", "Docker", dk))
        run_ = str(info.get("docker_running", ""))
        if run_.startswith("[exit") or "Cannot connect" in run_:
            rows.append(("FAIL", "Docker 守护", "未运行或当前用户无权访问 docker.sock"))
        else:
            rows.append(("OK", "Docker 守护", run_))

    dc = str(info.get("docker_compose", ""))
    has_v2 = "version" in dc.lower() and "docker compose" in dc.lower() and "not" not in dc.lower()[:20]
    has_v1 = "docker-compose version" in dc.lower()
    if has_v2:
        rows.append(("OK", "Compose", "v2 插件可用"))
    elif has_v1:
        rows.append(("WARN", "Compose", "只有 v1（docker-compose），部署时会用 v1 语法"))
    else:
        rows.append(("FAIL", "Compose", "都没有 —— 需装 docker-compose-plugin 或改用 docker run"))

    port = str(info.get("port_7864", ""))
    if port and port != "free" and "7864" in port:
        rows.append(("WARN", "端口", f"7864 已被占用：{port}"))
    else:
        rows.append(("OK", "端口", "7864 空闲"))

    return rows


def _int(v):
    try:
        return int(str(v).strip().split()[0])
    except (ValueError, IndexError):
        return None


# ── 部署 ────────────────────────────────────────────────────────────
def compose_for(image: str, port: int, admin_password: str) -> str:
    """按目标环境生成 compose。

    刻意不直接拷仓库里的 docker-compose.yml：那份带 build 段和一堆注释，
    在 1GB 内存的设备上误触发构建会直接卡死。
    """
    # YAML 是 JSON 的超集：用 JSON 字符串字面量当值，引号/反斜杠都能安全转义。
    # 直接拼 f-string 的话，密码里一个双引号就能把整个 compose 写坏。
    pwd = json.dumps(admin_password)
    return f"""# 由 scripts/remote.py 生成 —— 玩客云部署用，勿手工编辑
services:
  workbuddy:
    image: {image}
    container_name: workbuddy-allinone
    restart: unless-stopped
    ports:
      - "{port}:7864"
    environment:
      WB_ADMIN_USERNAME: admin
      WB_ADMIN_PASSWORD: {pwd}
      TZ: Asia/Shanghai
      # http = 进程内设备授权（不依赖 login 二进制）；binary 为旧路径
      WB_LOGIN_MODE: http
      WB_AUTO_START_UPSTREAM: "1"
      # S805 算力弱，默认的 60000 轮散列会让登录明显卡顿；20000 仍足够安全
      WB_PBKDF2_ITERS: "20000"
    volumes:
      - wb-data:/srv/data
      - wb-auths:/srv/upstream/auths
    logging:
      driver: json-file
      options:
        max-size: "5m"
        max-file: "2"
    # S805 上 Python 冷启动偏慢，start_period 比镜像默认值放宽。
    # 探针用 python 而不是 curl：容器里必定有 python，curl 则依赖构建时的 apt 是否成功。
    # 命令走 YAML 块标量（|）—— 里面既有单引号又有双引号，嵌进引号串里必然写坏 YAML。
    healthcheck:
      test:
        - CMD-SHELL
        - |
          python -c "import urllib.request,sys; r=urllib.request.urlopen('http://127.0.0.1:7864/api/healthz',timeout=5); sys.exit(0 if r.status==200 else 1)"
      interval: 30s
      timeout: 10s
      retries: 5
      start_period: 90s

volumes:
  wb-data:
  wb-auths:
"""


def deploy(rem: Remote, image: str | None, image_tar: Path | None, port: int,
           skip_pull: bool, admin_password: str) -> int:
    steps: list[tuple[str, bool, str]] = []

    def step(name, ok, detail=""):
        steps.append((name, ok, detail))
        print(f"  [{'OK ' if ok else 'FAIL'}] {name}" + (f" —— {detail}" if detail else ""))
        return ok

    print("\n== 准备目录 ==")
    rem.run(f"mkdir -p {REMOTE_DIR}", quiet=True)
    step(f"创建 {REMOTE_DIR}", True)

    if image_tar:
        print("\n== 上传离线镜像（可能较慢）==")
        remote_tar = f"{REMOTE_DIR}/image.tar.gz"
        data = image_tar.read_bytes()
        t0 = time.time()
        rem.upload(data, remote_tar)
        step(f"上传 {image_tar.name} ({len(data) // 1024 // 1024}MB)", True,
             f"{time.time() - t0:.0f}s")
        code, out, err = rem.run(f"docker load -i {remote_tar}", timeout=900)
        loaded = (out or err).strip().splitlines()[-1] if (out or err) else ""
        step("docker load", code == 0, loaded[:120])
        # 从 load 输出里推镜像名，避免用户手填错
        m = re.search(r"Loaded image: (\S+)", out or "")
        if m and not image:
            image = m.group(1)
        rem.run(f"rm -f {remote_tar}", quiet=True)
    elif image and not skip_pull:
        print("\n== 拉取镜像 ==")
        code, out, err = rem.run(f"docker pull {image}", timeout=900)
        step(f"docker pull {image}", code == 0, (err or out).strip()[-200:] if code else "")
        if code != 0:
            print("\n镜像拉取失败。国内拉 ghcr.io 经常被墙，改用离线包：")
            print("  python scripts/remote.py deploy ... --image-tar <CI 下载的 tar.gz>")
            return 1

    if not image:
        print("\n没有指定镜像（--image 或 --image-tar 二选一）")
        return 1

    print("\n== 写入 compose ==")
    rem.upload(compose_for(image, port, admin_password).encode("utf-8"),
               f"{REMOTE_DIR}/docker-compose.yml", mode=0o600)
    step("上传 docker-compose.yml", True, f"image={image} port={port}")

    print("\n== 启动 ==")
    dc = _compose_cmd(rem)
    code, out, err = rem.run(f"cd {REMOTE_DIR} && {dc} up -d", timeout=600)
    step(f"{dc} up -d", code == 0, (err or out).strip()[-300:] if code else "")

    if code != 0:
        return 1

    print("\n== 健康检查（最多等 150s，S805 冷启动偏慢）==")
    # 用 docker inspect 读镜像自带的 HEALTHCHECK 状态，不假设宿主机上有 curl
    status = ""
    deadline = time.time() + 150
    while time.time() < deadline:
        _, out, _ = rem.run(
            "docker inspect -f '{{.State.Health.Status}}' workbuddy-allinone 2>/dev/null",
            quiet=True)
        s = (out or "").strip()
        if s in ("healthy", "unhealthy"):
            status = s
            break
        if s:
            status = s
        time.sleep(6)

    if status != "healthy":
        # 兜底：直接在容器内打探针（老 docker 可能不报 health 字段）
        code, out, err = rem.run(
            "docker exec workbuddy-allinone python -c \"import urllib.request;"
            "r=urllib.request.urlopen('http://127.0.0.1:7864/api/healthz',timeout=5);"
            "print(r.status)\"", timeout=30, quiet=True)
        if code == 0 and "200" in (out or ""):
            status = "healthy"
            step("面板响应 /api/healthz", True, "容器内探针 200")
        else:
            step("面板响应 /api/healthz", False, f"status={status or 'unknown'} "
                                                 f"{(err or out).strip()[:160]}")
            _, logs, _ = rem.run("docker logs --tail 30 workbuddy-allinone 2>&1", timeout=30)
            print("\n== 容器日志（尾部 30 行）==")
            print((logs or "").strip())
    else:
        step("容器健康状态", True, "healthy")

    code, out, _ = rem.run(f"cd {REMOTE_DIR} && {dc} ps", timeout=60)
    print("\n== 容器状态 ==")
    print((out or "").strip())

    print("\n== 访问方式 ==")
    print(f"  http://{rem.host}:{port}/")
    print(f"  用户名：admin")
    print(f"  密　码：{admin_password}")
    print("  （密码已写入远程 docker-compose.yml，权限 0600；改密码请改该文件后 up -d）")

    failed = [s for s in steps if not s[1]]
    print(f"\n结果：{len(steps) - len(failed)}/{len(steps)} 步通过")
    return 1 if failed else 0


def _compose_cmd(rem: Remote) -> str:
    code, out, _ = rem.run("docker compose version", quiet=True)
    if code == 0 and "version" in (out or "").lower():
        return "docker compose"
    return "docker-compose"


# ── CLI ─────────────────────────────────────────────────────────────
def main() -> int:
    ap = argparse.ArgumentParser(description="玩客云远程部署工具")
    ap.add_argument("action", choices=["probe", "deploy"])
    ap.add_argument("--host", required=True)
    ap.add_argument("--user", default="root")
    ap.add_argument("--port", type=int, default=22)
    ap.add_argument("--password", help="不传则交互式输入")
    ap.add_argument("--key", dest="key_path", help="私钥路径（与密码二选一）")
    ap.add_argument("--image", help="镜像名，如 ghcr.io/wliky/workbuddy-allinone:armv7")
    ap.add_argument("--image-tar", help="本地离线镜像 tar.gz（CI artifact）")
    ap.add_argument("--app-port", type=int, default=7864, help="面板对外端口")
    ap.add_argument("--skip-pull", action="store_true", help="镜像已在机器上时跳过拉取")
    ap.add_argument("--admin-password", help="管理员密码；不传则随机生成并打印")
    ap.add_argument("--out", help="把报告写入文件")
    args = ap.parse_args()

    password = args.password
    if not password and not args.key_path:
        password = getpass.getpass(f"{args.user}@{args.host} 密码：")

    lines: list[str] = []

    def say(s=""):
        print(s)
        lines.append(s)

    try:
        rem = Remote(args.host, args.user, password, args.key_path, args.port)
    except Exception as exc:  # noqa: BLE001
        say(f"SSH 连接失败：{exc}")
        return 2

    try:
        say(f"已连接 {args.user}@{args.host}:{args.port}"
            + ("（放宽了 SSH 算法以兼容老旧 sshd）" if rem.legacy else ""))

        if args.action == "probe":
            say("\n== 环境探测 ==")
            info = probe(rem)
            for k, v in info.items():
                say(f"  {k:16} {v}")
            say("\n== 结论 ==")
            for level, item, desc in verdict(info):
                say(f"  [{level:4}] {item}: {desc}")
            say()
            say(json.dumps(info, ensure_ascii=False, indent=1))
            return 0

        if not args.image and not args.image_tar:
            say("deploy 需要 --image 或 --image-tar")
            return 2
        if args.image_tar and not Path(args.image_tar).exists():
            say(f"离线镜像不存在：{args.image_tar}")
            return 2
        # 在部署侧生成，而不是让容器随机打进日志：用户当场拿到，密码也不落日志
        admin_password = args.admin_password or secrets.token_urlsafe(14)
        return deploy(rem, args.image, Path(args.image_tar) if args.image_tar else None,
                      args.app_port, args.skip_pull, admin_password)
    finally:
        rem.close()
        if args.out:
            Path(args.out).write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
