"""LLM 客户端：走本机 magpie 路由（127.0.0.1:3425）。"""
from __future__ import annotations
import json
import os
import re
import threading
import time
from collections import deque
import requests
from core import config


# 最近若干次失败的简短描述。调用方（提案池）据此把「为什么 0 候选」写进
# 池的 error 字段 —— 否则 429 配额耗尽在日志里只表现为「0 个唯一候选」，
# 与「LLM 正常但提案全被去重吃掉」无法区分（2026-10-29 连续 3 轮踩坑）。
_LAST_ERRORS: deque[str] = deque(maxlen=8)
_FAIL_COUNT = 0
_ERR_LOCK = threading.Lock()


def _note_error(msg: str) -> None:
    global _FAIL_COUNT
    with _ERR_LOCK:
        _LAST_ERRORS.append(f"{time.strftime('%H:%M:%S')} {msg}")
        _FAIL_COUNT += 1


def failure_count() -> int:
    """累计失败次数（单调递增）。

    给调用方做「这段区间内到底有没有真失败」的判定：deque 会被覆盖，
    只看 last_error() 分不清是本轮失败还是几小时前的旧错误。
    """
    with _ERR_LOCK:
        return _FAIL_COUNT


def last_error() -> str | None:
    """最近一次失败的简短描述；成功调用不清空，便于事后归因。"""
    with _ERR_LOCK:
        return _LAST_ERRORS[-1] if _LAST_ERRORS else None


def recent_errors(limit: int = 3) -> list[str]:
    with _ERR_LOCK:
        return list(_LAST_ERRORS)[-limit:]


def chat(messages: list[dict], temperature: float = 0.7, max_tokens: int = 4000,
         timeout: int = 240, retries: int = 3) -> str:
    """调用本机模型路由（provider=magpie, 127.0.0.1:3425）。

    密钥取 ``MAGPIE_API_KEY``，地址取 ``QUANT_LLM_BASE_URL``（默认 3425）。
    9Router 已于 2026-10-30 从本机移除，旧的 ``NINEROUTER_API_KEY`` 已清理。

    上游 `free` 模型有时进入 reasoning 模式：思考过程会占用全部 max_tokens
    预算，导致 finish_reason=length 且 content 为空串（2026-10-21 实测：
    4000 token 全被 reasoning_tokens 吃掉）。此时加大预算重试即可拿到正常
    输出，因此这里对「空 content」按指数放大 max_tokens 重试。
    """
    # 硬闸：挖矿机不做任何 LLM 推理（店长定死）。提案由主控机生成后同步过去。
    # 放在最前面 —— 漏掉任何一条调用路径时，这里会立刻报错，
    # 而不是悄悄打一个 429 再被当成「上游抖动」重试。
    if os.environ.get("QUANT_LLM_DISABLED", "").strip() == "1":
        raise RuntimeError(
            "本机禁止 LLM 推理（QUANT_LLM_DISABLED=1）：提案池/新闻判断由主控机生成后同步"
        )

    if not config.has_llm():
        raise RuntimeError("缺少 MAGPIE_API_KEY")

    # 预算过小会让 reasoning 型响应把 token 全花在思考上，正文恒为空。
    budget = max(int(max_tokens), 16000)
    last_err = None
    for attempt in range(max(1, retries)):
        payload = {
            "model": config.get_main_model(),
            "messages": messages,
            "temperature": temperature,
            "max_tokens": budget,
        }
        try:
            r = requests.post(f"{config.LLM_BASE_URL}/chat/completions",
                              json=payload,
                              headers={"Content-Type": "application/json",
                                       "Authorization": f"Bearer {config.LLM_API_KEY}"},
                              timeout=timeout)
            if r.status_code in (502, 503, 504):
                last_err = RuntimeError(f"upstream {r.status_code}")
                time.sleep(5 * (attempt + 1))
                continue
            # 429：免费额度（workbuddy 每账号 350 credits/日）被打穿时上游会
            # 持续 429，不是秒级抖动。有 Retry-After 就照办，否则短退避后重试；
            # 三次都 429 就带着明确原因失败，别让调用方以为「LLM 正常但没提案」。
            if r.status_code == 429:
                ra = r.headers.get("Retry-After")
                try:
                    wait = min(max(float(ra), 2.0), 30.0) if ra else 0.0
                except (TypeError, ValueError):
                    wait = 0.0
                last_err = RuntimeError("upstream 429 rate limited / 配额耗尽")
                _note_error(f"429 (attempt {attempt + 1}) wait={wait:.0f}s")
                time.sleep(wait or 5 * (attempt + 1))
                continue
            r.raise_for_status()
            # 9router 的响应头不带 charset，requests 会按 ISO-8859-1 解码，
            # 中文正文直接变乱码（实测 "收到" → "æ¶å°"）。字节本身是 UTF-8，
            # 所以这里统一按 UTF-8 重新解码。
            r.encoding = "utf-8"
            content = _parse_completion(r.text)["choices"][0]["message"]["content"]
            if content and content.strip():
                return content
            # 空正文：多半是 thinking 吃光预算 → 加倍再试
            last_err = RuntimeError(f"empty content (max_tokens={budget})")
            budget = min(budget * 2, 60000)
            time.sleep(3)
        except (requests.RequestException, KeyError) as e:
            last_err = e
            time.sleep(5 * (attempt + 1))
    _note_error(f"failed after {retries}: {last_err}")
    raise RuntimeError(f"LLM 调用失败（{retries}次）: {last_err}")


def _parse_completion(text: str) -> dict:
    """解析 9router 的补全响应。

    上游偶尔会在正常 JSON 后面再追一段 SSE 尾巴（`data: [DONE]`），
    直接 json.loads 会报 "Extra data: line 1 column N"。这里先把
    `data:` 开头的行剥掉；仍失败时退回"取第一个完整 JSON 对象"。
    """
    text = text.strip()
    if text.startswith("data:"):
        # 纯 SSE：取最后一个合法 data 行
        for line in reversed(text.splitlines()):
            line = line.strip()
            if line.startswith("data:") and "[DONE]" not in line:
                text = line[5:].strip()
                break
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # 截断尾部噪音：找第一个 "{" 到最后一个 "}" 之间的片段
    i, j = text.find("{"), text.rfind("}")
    if i != -1 and j > i:
        return json.loads(text[i:j + 1])
    raise


def extract_json(text: str):
    """从 LLM 输出里抠出 JSON（容忍 ```json 包裹、多余文字、截断的数组）。"""
    text = text.strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if m:
        text = m.group(1).strip()
    for open_ch, close_ch in [("[", "]"), ("{", "}")]:
        i = text.find(open_ch)
        j = text.rfind(close_ch)
        if i != -1 and j != -1 and j > i:
            try:
                return json.loads(text[i:j + 1])
            except json.JSONDecodeError:
                pass
        # 尝试修复截断的数组：补齐收尾
        if i != -1:
            frag = text[i:]
            frag = _repair_truncated(frag, open_ch, close_ch)
            if frag:
                try:
                    return json.loads(frag)
                except json.JSONDecodeError:
                    pass
    return json.loads(text)


def _repair_truncated(frag: str, open_ch: str, close_ch: str) -> str | None:
    """把截断的 JSON 片段修好：去掉不完整的最后一个对象，补齐括号。"""
    if open_ch != "[":
        return None
    # 找到最后一个完整的 }（元素结尾）
    last = frag.rfind("}")
    if last == -1:
        return None
    body = frag[:last + 1]
    # 去掉尾部多余的逗号
    body = body.rstrip().rstrip(",")
    return body + "\n]"
