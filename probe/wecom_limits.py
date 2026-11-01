"""Probe WeCom group-robot message size limits empirically.

每发一条都会真的进群；失败（errcode != 0）的那条不会进群。
所以先发"很可能超限"的大消息：被拒绝 = 没打扰，还顺手确认了上限。
"""
import json
import os
import urllib.request

# 密钥不落盘：从环境变量读，别把 webhook 写进脚本再提交到公开仓库。
WEBHOOK = os.environ.get("WECOM_WEBHOOK_URL", "")
if not WEBHOOK:
    raise SystemExit("缺少环境变量 WECOM_WEBHOOK_URL（完整地址，含 ?key=）")


def post(payload: dict) -> dict:
    req = urllib.request.Request(
        WEBHOOK,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def bytes_utf8(s: str) -> int:
    return len(s.encode("utf-8"))


def probe(label: str, msgtype: str, build) -> None:
    payload = build()
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    body_len = len(payload[msgtype]["content"].encode("utf-8"))
    try:
        resp = post(payload)
    except Exception as e:  # noqa: BLE001
        print(f"{label:34s} content={body_len:5d}B req={len(raw):5d}B  EXC {e}")
        return
    print(f"{label:34s} content={body_len:5d}B req={len(raw):5d}B  -> {resp}")


def text_of(n_bytes: int) -> str:
    # 用中文填，1 个汉字 = 3 字节
    head = "【长度测试-请忽略】"
    pad = "测" * ((n_bytes - bytes_utf8(head)) // 3)
    return head + pad


def markdown_of(n_bytes: int) -> str:
    head = "## 长度测试-请忽略\n> markdown 探针\n\n"
    pad = "测" * ((n_bytes - bytes_utf8(head)) // 3)
    return head + pad


if __name__ == "__main__":
    print("=== text 类型探针（官方上限 2048 字节？）===")
    probe("text 2000B (应通过)", "text", lambda: {
        "msgtype": "text", "text": {"content": text_of(2000)}})
    probe("text 2600B (应被拒)", "text", lambda: {
        "msgtype": "text", "text": {"content": text_of(2600)}})

    print("\n=== markdown 类型探针（官方上限 4096 字节？）===")
    probe("markdown 3800B (应通过)", "markdown", lambda: {
        "msgtype": "markdown", "markdown": {"content": markdown_of(3800)}})
    probe("markdown 4400B (应被拒)", "markdown", lambda: {
        "msgtype": "markdown", "markdown": {"content": markdown_of(4400)}})
