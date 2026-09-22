from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_JEV_BASE_URL = "https://api.typesafe.ai/v1"
DEFAULT_JEV_MODEL = "jev-latest"
DEFAULT_MIN_CONFIDENCE = 0.6
DEFAULT_TIMEOUT_SECONDS = 15.0
DEFAULT_CONCURRENCY = 8


class JevError(RuntimeError):
    """Base exception for TypeSafe AI Jev integration."""


class JevAuthError(JevError):
    """Authentication or authorization failure (HTTP 401/403)."""


class JevRateLimitError(JevError):
    """Rate limit exceeded (HTTP 429)."""


class JevTimeoutError(JevError):
    """Network connection timeout or connection error."""


@dataclass
class JevQuestionResult:
    question_type: str
    choice: str | None = None
    score: int | None = None
    noul: float | None = None
    confidence: float = 1.0
    probabilities: dict[str, float] | None = None
    raw: dict[str, Any] | None = None


@dataclass
class JevEvaluation:
    verdict: str  # "keep" or "reject"
    is_usable: bool
    confidence: float
    standalone: bool
    content_type: str
    selling_value: int  # 0 - 100
    opening_suitability: int  # 0 - 100
    information_gain: int  # 0 - 100
    reason: str
    main_product_relevant: bool = True
    raw_response: dict[str, Any] | None = None


def make_choice_question(instructions: str, criteria: dict[str, str]) -> dict[str, Any]:
    return {
        "type": "choice",
        "instructions": instructions,
        "criteria": criteria,
    }


def make_score_question(instructions: str, criteria: list[str]) -> dict[str, Any]:
    return {
        "type": "score",
        "instructions": instructions,
        "criteria": criteria,
    }


def make_noul_question(instructions: str, criteria: dict[str, str] | None = None) -> dict[str, Any]:
    res: dict[str, Any] = {
        "type": "noul",
        "instructions": instructions,
    }
    if criteria:
        res["criteria"] = criteria
    return res


def build_candidate_audit_questions(main_product: str = "女装T恤") -> dict[str, Any]:
    """Build questions for comprehensive candidate semantic evaluation."""
    return {
        "is_usable": make_choice_question(
            instructions=f"判断该口播句子是否适合作为抖音短视频切片成片台词（主商品：{main_product}）。要求具备长效商品与穿搭价值，排除直播间现场即时互动与限时促销福利。",
            criteria={
                "yes": (
                    "可用（是）：专注服装本身的款式设计、面料做工、版型细节、上身穿搭建议、遮肉显瘦效果、真实用户好评或真实痛点解决等长效商品价值。"
                ),
                "no": (
                    "不可用（否）："
                    "1. 直播间强时效性促销/福利/库存（如：'前50名'、'库存只剩最后3单'、'预售链接'、'全没了'、'手速慢了'）；"
                    "2. 直播间现场互动与指令（如：'扣1上链接'、'大家去拍1号链接'、'打个有'）；"
                    "3. 设备调试、闲聊与纯水词寒暄（如：'灯光调一下'、'听得清吗'、'我喝口水大家稍等'）；"
                    "4. ASR识别严重错乱或文本读不通（如'一件提取'、'长蹄'、残缺断尾、主谓宾严重缺失）；"
                    "5. 明显属于非主商品的其它服饰单独介绍（如卖T恤时单独介绍马甲库存）。"
                ),
            },
        ),
        "standalone": make_noul_question(
            instructions="该句脱离原直播上下文后，是否能被短视频观众独立理解且句意完整（非残句断尾）？"
        ),
        "content_type": make_choice_question(
            instructions="该口播句子的核心内容属性归类",
            criteria={
                "selling_point": "主打核心卖点、设计亮点或品质承诺",
                "material": "面料做工、材质手感、定制罗口等工艺细节",
                "fit": "版型、遮肉显瘦效果、衣长遮臀等上身剪裁",
                "color": "颜色、尺码、规格选项",
                "styling": "搭配指南、穿搭建议或秋冬穿着场景",
                "proof": "销量表现、用户口碑、复购反馈",
                "reaction": "上身直接惊艳效果或真实情绪反应",
                "story": "选品背景或设计灵感",
                "stage_chatter": "直播间互动、催拍、场控指令",
                "inventory_logistics": "库存、发货、物流、预售连接",
                "secondary_product": "非主商品的搭配件或次要单品",
                "garbled": "ASR错字、口误或语病无法辨认",
                "fragment": "残句短语、前后脱节",
                "low_information": "低信息口头禅、空泛套话",
            },
        ),
        "selling_value": make_score_question(
            instructions="该句对短视频种草与促成购买的实际价值评分（0至4级）",
            criteria=[
                "0级：毫无价值的场控废话、ASR错词、跑题或纯水词",
                "1级：泛泛而谈的口头套话，信息量极低",
                "2级：有一定穿搭参考但较为普通的常规描述",
                "3级：明确具体的卖点、面料工艺、遮肉版型或搭配建议",
                "4级：极具说服力的核心痛点解决、强烈对比或重磅口碑证据",
            ],
        ),
        "opening_suitability": make_score_question(
            instructions="放在短视频前3-5秒作为吸睛黄金开头的适合度（0至4级）",
            criteria=[
                "0级：完全不适合开头，平淡报款、寒暄或依赖上文",
                "1级：普通的陈述句，缺乏停留理由",
                "2级：中规中矩的款式展示",
                "3级：有吸引力的穿搭效果或视觉展示",
                "4级：直击真实痛点（如领子易变形）、强烈反差或高价值悬念",
            ],
        ),
    }


class JevClient:
    """Zero-dependency TypeSafe AI Jev client using standard library urllib."""

    def __init__(
        self,
        api_key: str,
        base_url: str = DEFAULT_JEV_BASE_URL,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        default_model: str = DEFAULT_JEV_MODEL,
    ):
        self.api_key = (api_key or "").strip()
        url = (base_url or DEFAULT_JEV_BASE_URL).rstrip("/")
        if not url.endswith("/v1"):
            url = f"{url}/v1"
        self.endpoint = f"{url}/systemone"
        self.timeout = max(1.0, float(timeout))
        self.default_model = default_model or DEFAULT_JEV_MODEL

    def system_one(
        self,
        state: str,
        questions: dict[str, Any],
        model: str | None = None,
    ) -> dict[str, Any]:
        """Call POST /v1/systemone synchronously."""
        if not self.api_key:
            raise JevAuthError("未配置 TYPESAFE_API_KEY")

        payload = {
            "state": state,
            "model": model or self.default_model,
            "questions": questions,
        }
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            self.endpoint,
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
                "User-Agent": "agent-live-sliced-video/0.1.0",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                resp_bytes = resp.read()
                return json.loads(resp_bytes.decode("utf-8"))
        except urllib.error.HTTPError as err:
            if err.code in (401, 403):
                raise JevAuthError(f"TypeSafe API 鉴权失败 HTTP {err.code}") from None
            if err.code == 429:
                raise JevRateLimitError("TypeSafe API 触发速率限制 HTTP 429") from None
            err_body = err.read().decode("utf-8", errors="replace") if err.fp else ""
            raise JevError(f"TypeSafe API 返回错误 HTTP {err.code}: {err_body}") from None
        except (urllib.error.URLError, TimeoutError) as err:
            raise JevTimeoutError(f"TypeSafe API 请求超时或网络故障: {err}") from None
        except Exception as exc:
            raise JevError(f"TypeSafe API 未知异常: {exc}") from None

    def evaluate_candidate(
        self,
        text: str,
        main_product: str = "女装T恤",
        model: str | None = None,
        min_confidence: float = DEFAULT_MIN_CONFIDENCE,
    ) -> JevEvaluation:
        """Evaluate a single candidate sentence using Jev primitives."""
        questions = build_candidate_audit_questions(main_product)
        response = self.system_one(state=text, questions=questions, model=model)
        answers = response.get("answers") or {}

        # 1. is_usable answer
        usable_ans = answers.get("is_usable") or {}
        choice = usable_ans.get("choice")
        confidence = float(usable_ans.get("confidence", 1.0))
        is_usable = (choice == "yes")

        # 2. standalone answer
        standalone_ans = answers.get("standalone") or {}
        noul_val = float(standalone_ans.get("noul", 1.0))
        standalone = noul_val >= 0.5

        # 3. content_type answer
        ctype_ans = answers.get("content_type") or {}
        content_type = ctype_ans.get("choice") or ("selling_point" if is_usable else "low_information")

        # 4. selling_value answer (0..4 -> 0..100)
        sval_ans = answers.get("selling_value") or {}
        score_val = sval_ans.get("score")
        if score_val is not None:
            selling_value = min(100, int(score_val) * 25)
        else:
            selling_value = 75 if is_usable else 10

        # 5. opening_suitability answer (0..4 -> 0..100)
        open_ans = answers.get("opening_suitability") or {}
        open_score = open_ans.get("score")
        if open_score is not None:
            opening_suitability = min(100, int(open_score) * 25)
        else:
            opening_suitability = 50 if is_usable else 10

        information_gain = 60 if is_usable else 10

        # Confidence gating: if confidence is below threshold, downgrade or reject
        if is_usable and confidence < min_confidence:
            verdict = "reject"
            reason = f"Jev 判定可用但置信度不足 ({confidence:.2f} < {min_confidence:.2f})"
        elif is_usable:
            verdict = "keep"
            reason = f"Jev 语义审核判定为有效卖点 (置信度 {confidence:.2f})"
        else:
            verdict = "reject"
            reason = f"Jev 判定为非切片有效内容 ({content_type}，置信度 {confidence:.2f})"

        return JevEvaluation(
            verdict=verdict,
            is_usable=is_usable,
            confidence=confidence,
            standalone=standalone,
            content_type=content_type,
            selling_value=selling_value,
            opening_suitability=opening_suitability,
            information_gain=information_gain,
            reason=reason,
            main_product_relevant=(content_type != "secondary_product"),
            raw_response=response,
        )
