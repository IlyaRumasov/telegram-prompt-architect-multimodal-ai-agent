#!/usr/bin/env python3
"""Тести маршрутизації та логіки Telegram Prompt Architect.

Скрипт збирає тестову копію флоу, де всі зовнішні вузли (Telegram, OpenAI, OpenRouter,
Data Table, агент) замінені заглушками з готовими відповідями, а Telegram Trigger замінений
на Webhook. Копія імпортується в n8n, активується, отримує тестові апдейти з cases.json,
після чого перевіряється, які вузли відпрацювали та які значення вони видали. Наприкінці
тестова копія видаляється. Жодних реальних викликів зовнішніх сервісів немає.

Запуск:
    python tests/run_tests.py --url https://<ваш-n8n> --key-file .n8n-key.txt
Ключ API n8n лежить у файлі (не в аргументах і не в репозиторії).
"""
import argparse
import copy
import json
import sys
import time
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXTERNAL = {
    "n8n-nodes-base.telegram", "@n8n/n8n-nodes-langchain.openAi", "n8n-nodes-base.httpRequest",
    "n8n-nodes-base.dataTable", "@n8n/n8n-nodes-langchain.agent", "@n8n/n8n-nodes-langchain.googleGemini",
}
DROP = {"@n8n/n8n-nodes-langchain.lmChatGoogleGemini", "@n8n/n8n-nodes-langchain.lmChatOpenRouter", "@n8n/n8n-nodes-langchain.memoryBufferWindow", "n8n-nodes-base.stickyNote"}
STUB = """const s = (($('Webhook').first().json.body.stubs) || {})[%s];
if (s === undefined) return [{ json: { ok: true } }];
const PNG = 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==';
return (Array.isArray(s) ? s : [s]).map(x => {
  const { _binary, ...json } = x;
  return _binary ? { json, binary: { [_binary]: { data: PNG, mimeType: 'image/png', fileName: 'stub.png' } } } : { json };
});"""
TEST_ADMIN_ID = "999"


class Api:
    def __init__(self, url, key):
        self.url, self.key = url.rstrip("/"), key

    def call(self, method, path, body=None, raw=False):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.url + path, data=data, method=method, headers={
            "X-N8N-API-KEY": self.key, "Content-Type": "application/json", "User-Agent": "curl/8.7.1"})
        with urllib.request.urlopen(req, timeout=60) as r:
            txt = r.read().decode()
        return txt if raw else (json.loads(txt) if txt else {})


def build_harness(wf, path):
    w = copy.deepcopy(wf)
    first = w["connections"]["Telegram Trigger"]["main"][0][0]["node"]  # перший вузол після тригера (Set Config)
    w["name"] = "[TEST-HARNESS] Telegram Prompt Architect"
    nodes, names = [], set()
    for n in w["nodes"]:
        if n["type"] in DROP:
            continue
        n.pop("credentials", None)
        if n["type"] == "n8n-nodes-base.telegramTrigger":
            n.update(type="n8n-nodes-base.webhook", typeVersion=2, name="Webhook", webhookId=str(uuid.uuid4()),
                     parameters={"httpMethod": "POST", "path": path, "options": {}})
        elif n["type"] in EXTERNAL:
            n.update(type="n8n-nodes-base.code", typeVersion=2, parameters={"jsCode": STUB % json.dumps(n["name"])})
            n.pop("webhookId", None)
        if n["name"] == first:
            for a in n["parameters"]["assignments"]["assignments"]:
                if a["name"] == "admin_chat_id":
                    a["value"] = TEST_ADMIN_ID
        nodes.append(n)
        names.add(n["name"])
    w["nodes"] = nodes
    nodes.append({"parameters": {"jsCode": "return [{ json: { ...$input.first().json.body.update } }];"}, "type": "n8n-nodes-base.code",
                  "typeVersion": 2, "position": [100, 500], "id": str(uuid.uuid4()), "name": "Unwrap"})
    conns = {}
    for src, v in w["connections"].items():
        if src not in names and src != "Telegram Trigger":
            continue
        s = "Webhook" if src == "Telegram Trigger" else src
        m = {}
        for kind, outs in v.items():
            if kind != "main":
                continue
            m[kind] = [[c for c in out if c["node"] in names] for out in outs]
        conns[s] = m
    conns["Webhook"] = {"main": [[{"node": "Unwrap", "type": "main", "index": 0}]]}
    conns["Unwrap"] = {"main": [[{"node": first, "type": "main", "index": 0}]]}
    w["connections"] = conns
    w["settings"] = {"executionOrder": "v1"}
    return w


def render(case):
    now = int(time.time() * 1000)
    iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    t = json.dumps(case, ensure_ascii=False)
    t = t.replace('"$NOW"', str(now)).replace("$ISO_NOW", iso).replace("$A8000", "a" * 8000)
    return json.loads(t)


def first_json(run, node):
    for out in (run[node][-1].get("data") or {}).get("main", []):
        if out:
            return out[0]["json"]
    return {}


def dig(obj, path):
    for p in path.split("."):
        obj = obj.get(p) if isinstance(obj, dict) else None
    return obj


def check(case, run):
    errs = []
    for n in case.get("expect", []):
        if n not in run:
            errs.append(f"вузол не відпрацював: {n}")
    for n in case.get("not_expect", []):
        if n in run:
            errs.append(f"вузол не мав відпрацювати: {n}")
    for n, vals in case.get("values", {}).items():
        if n not in run:
            errs.append(f"немає вузла для перевірки значень: {n}")
            continue
        j = first_json(run, n)
        for path, want in vals.items():
            got = dig(j, path)
            ok = (want[1:] in str(got)) if isinstance(want, str) and want.startswith("~") else got == want
            if not ok:
                errs.append(f"{n}.{path}: очікувалось {want!r}, отримано {str(got)[:80]!r}")
    for n, cnt in case.get("counts", {}).items():
        got = sum(len(o) for o in run[n][-1]["data"]["main"]) if n in run else 0
        if got != cnt:
            errs.append(f"{n}: елементів {got}, очікувалось {cnt}")
    return errs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--key-file", required=True)
    ap.add_argument("--workflow", default=str(ROOT / "workflows" / "telegram-prompt-architect.json"))
    ap.add_argument("--cases", default=str(Path(__file__).with_name("cases.json")))
    ap.add_argument("--only", default="")
    ap.add_argument("--keep", action="store_true", help="не видаляти тестову копію")
    a = ap.parse_args()
    api = Api(a.url, Path(a.key_file).read_text().strip())
    wf = json.load(open(a.workflow, encoding="utf-8"))
    cases = json.load(open(a.cases, encoding="utf-8"))
    if a.only:
        cases = [c for c in cases if a.only in c["name"]]
    path = "pa-test-" + uuid.uuid4().hex[:10]
    wid = api.call("POST", "/api/v1/workflows", build_harness(wf, path))["id"]
    failed = 0
    try:
        api.call("POST", f"/api/v1/workflows/{wid}/activate", {})
        time.sleep(2)
        last = (api.call("GET", f"/api/v1/executions?workflowId={wid}&limit=1").get("data") or [{}])[0].get("id")
        for raw in cases:
            c = render(raw)
            body = {"update": c["update"], "stubs": c.get("stubs", {})}
            api.call("POST", f"/webhook/{path}", body, raw=True)
            ex = None
            for _ in range(40):
                time.sleep(0.5)
                d = (api.call("GET", f"/api/v1/executions?workflowId={wid}&limit=1").get("data") or [{}])[0]
                if d.get("id") and d.get("id") != last and d.get("status") not in ("running", "new", "waiting"):
                    ex = d
                    break
            if not ex:
                print(f"FAIL {c['name']}: виконання не з'явилось")
                failed += 1
                continue
            last = ex["id"]
            data = api.call("GET", f"/api/v1/executions/{ex['id']}?includeData=true")
            rd = data["data"]["resultData"]
            errs = check(c, rd["runData"])
            if rd.get("error"):
                errs.append("помилка виконання: " + str(rd["error"].get("message"))[:120])
            print(("PASS " if not errs else "FAIL ") + c["name"])
            for e in errs:
                print("     -", e)
            failed += bool(errs)
    finally:
        if not a.keep:
            try:
                api.call("POST", f"/api/v1/workflows/{wid}/deactivate", {})
            except Exception:
                pass
            api.call("DELETE", f"/api/v1/workflows/{wid}")
    print(f"\nПройдено {len(cases) - failed} з {len(cases)}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
