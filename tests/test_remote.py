#!/usr/bin/env python3
"""scripts/remote.py 的单元测试（不需要 SSH，也不需要真的玩客云）。

覆盖的是「最容易写错又最难在真机上排查」的两块：
  1. 生成的 docker-compose.yml 是不是合法 YAML —— 写坏了的表现是容器起不来，
     而报错指向 compose 解析，离真正的诱因（某个字符没转义）很远。
  2. 环境判定（verdict）在各种硬件/软件组合下的结论是否符合预期。

跑法：python tests/test_remote.py
"""
from __future__ import annotations

import sys
from pathlib import Path

PROJ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJ / "scripts"))

import remote  # noqa: E402

fails: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'OK ' if ok else 'FAIL'}] {name}" + (f" —— {detail}" if detail else ""))
    if not ok:
        fails.append(f"{name}: {detail}")


# ── 1. compose 生成 ──────────────────────────────────────────────────
def test_compose() -> None:
    print("\n--- 生成的 compose ---")
    # 密码里刻意塞进引号与反斜杠：这是真实用户会做的事，也是最容易写坏 YAML 的输入
    tricky = 'p@ss"wo\\rd'
    c = remote.compose_for("ghcr.io/wliky/workbuddy-allinone:armv7", 7864, tricky)

    try:
        import yaml
    except ImportError:
        # 没 pyyaml 时退化成文本级校验，只测最容易坏的那两行
        hc = [l for l in c.splitlines() if "CMD-SHELL" in l or "urlopen" in l]
        check("无 pyyaml，跳过结构化校验", True)
        check("健康探针指向 /api/healthz", any("/api/healthz" in l for l in hc), str(hc))
        return

    d = yaml.safe_load(c)
    svc = d["services"]["workbuddy"]

    check("整体是合法 YAML", isinstance(d, dict))
    check("端口映射正确", svc["ports"] == ["7864:7864"], str(svc["ports"]))
    check("复杂密码原样保留", svc["environment"]["WB_ADMIN_PASSWORD"] == tricky,
          svc["environment"]["WB_ADMIN_PASSWORD"])
    check("没有编造的环境变量 WB_TZ", "WB_TZ" not in svc["environment"])
    check("登录模式是进程内 http", svc["environment"].get("WB_LOGIN_MODE") == "http")

    hc = svc["healthcheck"]["test"]
    check("探针命令存在", isinstance(hc, list) and len(hc) == 2, str(hc)[:80])
    check("探针打的是 /api/healthz", "/api/healthz" in hc[1], hc[1][:80])
    check("探针不依赖 curl", "curl" not in hc[1])

    # 换端口也要跟着变
    c2 = remote.compose_for("img:tag", 9000, "x")
    check("自定义端口生效", yaml.safe_load(c2)["services"]["workbuddy"]["ports"] == ["9000:7864"])


# ── 2. 环境判定 ──────────────────────────────────────────────────────
BASE = {
    "arch": "armv7l", "mem_mb": "2048", "docker": "Docker version 20.10.5",
    "docker_running": "20.10.5",
    "docker_compose": "Docker Compose version v2.20.0\ndocker-compose version 1.29.2",
    "port_7864": "free",
}

CASES = [
    ("标准 armv7（2GB）", {},
     {"架构": "OK", "内存": "OK", "Docker": "OK", "Compose": "OK", "端口": "OK"}),
    ("刷机包是 64 位", {"arch": "aarch64"}, {"架构": "WARN"}),
    ("armv6 无法部署", {"arch": "armv6l"}, {"架构": "FAIL"}),
    ("未知架构", {"arch": "mips"}, {"架构": "WARN"}),
    ("1GB 玩客云常见值", {"mem_mb": "1000"}, {"内存": "WARN"}),
    ("512MB 版本", {"mem_mb": "480"}, {"内存": "FAIL"}),
    ("内存读不出来", {"mem_mb": ""}, {}),
    ("没装 docker",
     {"docker": "[exit 127] docker: not found", "docker_running": "[exit 1] x"},
     {"Docker": "FAIL"}),
    ("docker 守护没起来", {"docker_running": "Cannot connect to the Docker daemon"},
     {"Docker": "OK", "Docker 守护": "FAIL"}),
    ("只有 compose v1", {"docker_compose": "docker-compose version 1.29.2"},
     {"Compose": "WARN"}),
    ("compose 都没有", {"docker_compose": "[exit 127] not found"}, {"Compose": "FAIL"}),
    ("7864 已被占用", {"port_7864": "LISTEN 0 128 :::7864"}, {"端口": "WARN"}),
]


def test_verdict() -> None:
    print("\n--- 环境判定 ---")
    for name, patch, expect in CASES:
        got = {item: lvl for lvl, item, _ in remote.verdict({**BASE, **patch})}
        for item, lvl in expect.items():
            check(f"{name} → {item}={lvl}", got.get(item) == lvl, f"实得 {got.get(item)}")


def test_helpers() -> None:
    print("\n--- 辅助函数 ---")
    check("_int 正常值", remote._int("1000") == 1000)
    check("_int 空值", remote._int("") is None)
    check("_int 非数字", remote._int("abc") is None)
    check("_int 带单位", remote._int("996 MB") == 996)


if __name__ == "__main__":
    test_compose()
    test_verdict()
    test_helpers()
    print(f"\n结果：{'全部通过' if not fails else f'{len(fails)} 项失败'}")
    for f in fails:
        print("  -", f)
    sys.exit(1 if fails else 0)
