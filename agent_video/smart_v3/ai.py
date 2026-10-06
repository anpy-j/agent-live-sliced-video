"""V3-only model transport and contracts; configuration never touches other stores."""
from __future__ import annotations

import json
import math
import os
import urllib.request

ROLES = {"hook", "result", "pain", "fit", "material", "styling", "proof", "scene", "close"}


def score(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 1:
        raise ValueError(f"Invalid {name}: expected finite number 0..1")
    return float(value)


def validate_profiles(raw, units):
    if not isinstance(raw, dict) or not isinstance(raw.get("profiles"), list):
        raise ValueError("Model must return profiles array")
    expected = {u["id"] for u in units}
    seen = set()
    for p in raw["profiles"]:
        if not isinstance(p, dict) or p.get("id") not in expected or p["id"] in seen:
            raise ValueError("Missing, duplicate or unknown profile id")
        seen.add(p["id"])
        for key in ("usable", "independent"):
            if type(p.get(key)) is not bool:
                raise ValueError(f"Invalid {key}")
        if not isinstance(p.get("role"), list) or any(r not in ROLES for r in p["role"]):
            raise ValueError("Invalid role array")
        for key in ("topic", "claim_cluster", "visual_need", "reason"):
            if not isinstance(p.get(key), str) or not p[key].strip():
                raise ValueError(f"Missing {key}")
        for key in ("strength", "hook_strength", "product_relevance"):
            p[key] = score(p.get(key), key)
        if not isinstance(p.get("dependency"), list) or any(d not in expected or d == p["id"] for d in p["dependency"]):
            raise ValueError("Invalid dependency")
    if seen != expected:
        raise ValueError("Model omitted sentence profiles")
    return raw["profiles"]


def validate_review(raw, ids):
    if not isinstance(raw, dict) or type(raw.get("passed")) is not bool or not isinstance(raw.get("issues"), list):
        raise ValueError("Invalid whole-film review")
    for issue in raw["issues"]:
        if (not isinstance(issue, dict) or issue.get("unit_id") not in ids
                or not isinstance(issue.get("reason"), str) or not issue["reason"].strip()):
            raise ValueError("Invalid review issue")
    if raw["passed"] == bool(raw["issues"]):
        raise ValueError("Inconsistent review verdict")
    return raw


class JsonAI:
    def __call__(self, kind, payload):
        key = os.environ.get("SMART_V3_API_KEY", "")
        model = os.environ.get("SMART_V3_MODEL", "")
        if not key or not model:
            raise ValueError("Set SMART_V3_API_KEY and SMART_V3_MODEL before starting V3")
        base = os.environ.get("SMART_V3_BASE_URL", "https://api.openai.com/v1").rstrip("/")
        instructions = {
            "profile": "理解商品和完整句上下文。仅输出 JSON {profiles:[{id,usable:boolean,role:[]或hook/result/pain/fit/material/styling/proof/scene/close的多值,topic,claim_cluster:同一信息点同一标识,strength:0..1,hook_strength:0..1,independent:boolean,dependency:[句id],product_relevance:0..1,visual_need,reason}]}。每句恰好一个画像，不要求所有角色存在。搭配提及副商品不能误删。不要编造画面信息。",
            "review": "按提供的最终顺序复审整片，检查重复、突兀跳题、指代缺失、连续弱句、开头回商品过慢。画面信息未知必须明确未知，不能虚构。输出 JSON {passed:boolean,issues:[{unit_id,reason}],visual_assessment:string}。通过时 issues 必须为空，否则指出需替换的完整句。",
        }
        body = {"model": model, "messages": [{"role": "system", "content": instructions[kind]},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
                "response_format": {"type": "json_object"}}
        req = urllib.request.Request(base + "/chat/completions", data=json.dumps(body).encode(),
                                     headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=180) as response:
            result = json.load(response)
        return json.loads(result["choices"][0]["message"]["content"])
