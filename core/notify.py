# -*- coding: utf-8 -*-
"""
AlphaDesk · 通知渠道（事件化）

对标结论（调研报告 A6 / C3 项）：通知应当由服务端按事件触发，而不是只在
浏览器里提示。本项目在服务端常驻策略跟踪引擎里产生事件，因此通知器挂在
引擎上，浏览器关掉也照样触达。

支持：
  · webhook：POST JSON 到自定义地址（企业微信 / 钉钉 / Slack / 自建服务均可）
  · 控制台：仅在开启 echo 时打印，便于本地调试
  · 级别过滤：默认只推「成交、平仓、止损止盈、资金不足、异常」
"""

from __future__ import annotations

import json
import threading
import time
import urllib.request

DEFAULT_EVENTS = ["on_fill", "on_exit", "on_skip", "on_error"]

EVENT_TEXT = {
    "on_run_start": "任务启动",
    "on_bar": "K线推进",
    "on_signal": "产生信号",
    "on_fill": "成交建仓",
    "on_exit": "平仓",
    "on_skip": "信号被跳过",
    "on_error": "引擎异常",
    "on_run_revised": "配置调整",
    "on_run_paused": "任务暂停",
    "on_resume": "任务恢复",
}

# 各事件的默认推送级别：error 级别的事件不参与「静默」过滤
EVENT_LEVEL = {
    "on_error": "error",
    "on_skip": "warn",
    "on_exit": "info",
    "on_fill": "info",
}


def describe(event, run, payload):
    """把事件翻译成一句人话，作为通知正文"""
    name = run.get("name") or run.get("code") or ""
    code = run.get("code") or ""
    market = "美股" if run.get("market") == "us" else "A股"
    label = EVENT_TEXT.get(event, event)
    p = payload or {}
    if event == "on_fill":
        return "%s %s %s 以 %s 成交 %s 股（阶段：%s）" % (
            label, market, code, _f(p.get("price")), p.get("qty"),
            "实时" if p.get("phase") == "live" else "回溯")
    if event == "on_exit":
        return "%s %s %s：%s，盈亏 %s（%s%%）" % (
            label, market, code, p.get("reason") or "信号",
            _f(p.get("pnl")), _f(p.get("pnlPct")))
    if event == "on_skip":
        return "%s %s %s：%s" % (label, market, code, p.get("reason") or "")
    if event == "on_error":
        return "%s %s %s：%s" % (label, market, code, p.get("error") or "")
    if event == "on_signal":
        return "%s %s %s：%s信号 @ %s" % (
            label, market, code, "买入" if p.get("side") == "buy" else "卖出", _f(p.get("price")))
    if event == "on_run_revised":
        return "%s %s %s：调整 %s%s" % (
            label, market, code, "、".join(p.get("changed") or []),
            "（已重置并重新回溯）" if p.get("reset") else "")
    return "%s %s %s（%s）" % (label, market, code, name)


def _f(v, nd=2):
    try:
        return ("%%.%df" % nd) % float(v)
    except (TypeError, ValueError):
        return "-"


class Notifier:
    """事件通知器：Runner 的 on_* 事件统一走这里"""

    def __init__(self, store=None, timeout=6, echo=False):
        self.store = store
        self.timeout = timeout
        self.echo = echo
        self._lock = threading.RLock()
        self.last = {"ts": None, "ok": None, "status": None, "error": None, "url": None}

    # ------------------------------------------------------------ 配置读写

    def settings(self):
        if not self.store:
            return {"webhook": "", "events": list(DEFAULT_EVENTS), "enabled": False}
        url = self.store.meta_get("notify_webhook", "") or ""
        events = self.store.meta_get("notify_events", None) or list(DEFAULT_EVENTS)
        return {"webhook": url, "events": events, "enabled": bool(url),
                "last": self.last}

    def save_settings(self, patch):
        if not self.store:
            raise RuntimeError("未配置存储，无法保存通知设置")
        patch = patch or {}
        if "webhook" in patch:
            url = str(patch.get("webhook") or "").strip()[:400]
            if url and not (url.startswith("http://") or url.startswith("https://")):
                raise RuntimeError("Webhook 地址需以 http:// 或 https:// 开头")
            self.store.meta_set("notify_webhook", url)
        if "events" in patch:
            events = [e for e in (patch.get("events") or []) if e in EVENT_TEXT]
            self.store.meta_set("notify_events", events)
        return self.settings()

    # ------------------------------------------------------------ 事件入口

    def __call__(self, event, run, payload):
        self.emit(event, run, payload)

    def emit(self, event, run, payload):
        settings = self.settings()
        events = settings.get("events") or DEFAULT_EVENTS
        url = (run or {}).get("notify") or settings.get("webhook") or ""
        if event not in events:
            return False
        text = describe(event, run, payload)
        if self.echo:
            print("[notify] %s | %s" % (event, text))
        if not url:
            return False
        body = {
            "event": event,
            "level": EVENT_LEVEL.get(event, "info"),
            "title": "AlphaDesk · %s" % (run.get("name") or run.get("code") or ""),
            "text": text,
            "time": int(time.time() * 1000),
            "run": {"id": (run or {}).get("id"), "market": (run or {}).get("market"),
                    "code": (run or {}).get("code"), "name": (run or {}).get("name"),
                    "strategy": (run or {}).get("strategy")},
            "payload": payload or {},
        }
        return self._post(url, body)

    def test(self, url=None):
        settings = self.settings()
        target = (url or settings.get("webhook") or "").strip()
        if not target:
            raise RuntimeError("请先填写 Webhook 地址")
        body = {
            "event": "test",
            "level": "info",
            "title": "AlphaDesk 通知测试",
            "text": "这是一条测试消息，收到即表示通知通道已打通。",
            "time": int(time.time() * 1000),
            "run": {},
            "payload": {},
        }
        ok = self._post(target, body)
        return {"ok": ok, "url": target, "last": self.last}

    def _post(self, url, body):
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        last_err = None
        for attempt in range(2):
            try:
                req = urllib.request.Request(
                    url, data=data, method="POST",
                    headers={"Content-Type": "application/json; charset=utf-8",
                             "User-Agent": "AlphaDesk-Notifier/1.0"})
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    status = getattr(resp, "status", 200)
                    resp.read()
                with self._lock:
                    self.last = {"ts": int(time.time() * 1000), "ok": True,
                                 "status": status, "error": None, "url": url}
                if self.store:
                    try:
                        self.store.meta_set("notify_last_ok", self.last)
                    except Exception:  # noqa: BLE001
                        pass
                return True
            except Exception as exc:  # noqa: BLE001
                last_err = str(exc)[:200]
                time.sleep(0.5)
        with self._lock:
            self.last = {"ts": int(time.time() * 1000), "ok": False,
                         "status": None, "error": last_err, "url": url}
        if self.store:
            try:
                self.store.meta_set("notify_last_ok", self.last)
            except Exception:  # noqa: BLE001
                pass
        return False


__all__ = ["Notifier", "describe", "EVENT_TEXT", "DEFAULT_EVENTS"]
