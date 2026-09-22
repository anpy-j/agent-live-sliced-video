# -*- coding: utf-8 -*-
"""Quiet single-source production entrypoint for the bundled slicing engine.

The process prepares a bounded selection packet, optionally waits for picks.json,
then performs alignment, validation, rendering, previews and QC without exposing
child-process logs to the model context.

成片默认值对齐账号标准：1440x2560 / 源帧率 / CRF16 preset slow / AAC 256k /
响度目标 -6.5 LUFS（单遍实测落在 -7.3~-7.8，与素材库现有成片一致）。
旧版默认 30fps + veryfast 并把竖屏源缩到 1080 宽，出来的成片与标准不符。
"""
import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from preflight import check_environment


ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"

# 词表配置：run_slice 默认用随包的账号级红线词表（价格/现货/发货/快飞等按硬禁处理），
# 通过环境变量传给所有子进程，避免每个底层脚本各自维护一份。
VOCAB_PROFILE_ENV = "DOUYIN_VOCAB_PROFILE"

# 单段时长上限（与 validate_timeline.py 对齐）。超过这个值的
# 选段下游一定会被门禁拦下，而且还可能让 cuts 的局部窗口退化，所以在入口就拦。
MAX_PICK_SECONDS = 8.0
ALLOWED_ROLES = {"hook", "result", "pain", "proof", "fit", "material", "craft",
                 "color", "styling", "scene", "demo", "close", "bridge",
                 "personality", "story", "reaction", "visual"}
ALLOWED_HOOKS = {"hook_A", "hook_B", "hook_C"}

# 失败时去哪个报告取问题清单（步骤 -> 报告文件）。
STEP_REPORTS = {"validate": "validation.json", "audit": "bounds_report.json",
                "audio_acceptance": "audio_acceptance.json",
                "validate_video_mapping_body": "dual_body_report.json",
                "qc_body": "qc/body/qc_report.json"}


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def file_sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def tail(value, limit=1200):
    value = (value or "").strip()
    return value[-limit:]


class PipelineFailure(RuntimeError):
    def __init__(self, step, returncode, log):
        super().__init__(f"{step} failed ({returncode})")
        self.step = step
        self.returncode = returncode
        self.log = str(log)


class QuietRunner:
    def __init__(self, logdir, timeout=7200):
        self.logdir = Path(logdir)
        self.logdir.mkdir(parents=True, exist_ok=True)
        self.steps = []
        self.timeout = timeout

    def run(self, step, command):
        started = time.monotonic()
        log = self.logdir / f"{len(self.steps) + 1:02d}_{step}.log"
        try:
            result = subprocess.run([str(x) for x in command], capture_output=True, text=True,
                                    encoding="utf-8", errors="replace", timeout=self.timeout)
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout.decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
            stderr = exc.stderr.decode("utf-8", "replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
            log.write_text(stdout + ("\n" if stdout else "") + stderr +
                           f"\nTIMEOUT after {self.timeout}s", encoding="utf-8")
            self.steps.append({"name": step, "seconds": round(time.monotonic() - started, 2),
                               "ok": False, "timeout": self.timeout, "log": str(log)})
            raise PipelineFailure(step, 124, log)
        log.write_text((result.stdout or "") + ("\n" if result.stdout else "") +
                       (result.stderr or ""), encoding="utf-8")
        item = {"name": step, "seconds": round(time.monotonic() - started, 2),
                "ok": result.returncode == 0}
        if result.returncode:
            item["log"] = str(log)
            self.steps.append(item)
            raise PipelineFailure(step, result.returncode, log)
        self.steps.append(item)
        return result


def lightweight_identity(path):
    path = Path(path)
    stat = path.stat()
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        digest.update(handle.read(1024 * 1024))
        if stat.st_size > 1024 * 1024:
            handle.seek(max(0, stat.st_size - 1024 * 1024))
            digest.update(handle.read(1024 * 1024))
    return {"path": str(path.resolve()), "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns, "edge_sha256": digest.hexdigest()}


def cached_artifact(manifest, key):
    manifest = Path(manifest)
    try:
        data = read_json(manifest)
        output = Path(data["output"])
    except Exception:
        return None
    if data.get("key") == key and output.is_file() and output.stat().st_size > 0:
        return output
    return None


def save_artifact(manifest, key, output):
    write_json(manifest, {"key": key, "output": str(Path(output).resolve())})


def render_key(timeline, media, args):
    return {"version": 1, "timeline_sha256": file_sha256(timeline),
            "source": lightweight_identity(media),
            "renderer_sha256": file_sha256(SCRIPTS / "render_dual.py"),
            "width": args.width, "height": args.height, "fps": args.fps,
            "crf": args.crf, "preset": args.preset,
            "audio_bitrate": args.audio_bitrate, "loudness": args.loudness,
            "allow_upscale": args.allow_upscale}


def media_duration(path):
    result = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                             "-of", "default=nw=1:nk=1", str(path)], capture_output=True,
                            text=True, encoding="utf-8", errors="replace")
    if result.returncode:
        raise RuntimeError("ffprobe could not read the source media")
    return float(result.stdout.strip())


def adaptive_limits(seconds):
    """候选不再设条数上限：脚本只筛确定性垃圾，可用性由 AI 分批逐条审核。"""
    minutes = seconds / 60.0
    if minutes <= 15:
        return {"mode": "fast", "overview_frames": 16, "candidate_limit": 0}
    if minutes <= 35:
        return {"mode": "fast", "overview_frames": 20, "candidate_limit": 0}
    return {"mode": "fast", "overview_frames": 24, "candidate_limit": 0}


def normalize_picks(path, media, output, max_pick_seconds=MAX_PICK_SECONDS):
    """校验并归一化 picks.json，返回 hook 模块名列表。

    - `src` 只在缺失时补 1。原实现无条件把 `src` 改写成 1，多源 picks 传进来会被
      静默改成单源、切点全错却不报错；现在显式给出别的源直接拒绝。
    - 单个完整口播单元在入口先查一遍（完整声音硬上限 8s，与编排门禁一致）。
      超长选段下游必然失败，而且会让 cuts 的局部窗口退化，在这里失败能省掉
      整条渲染与一次全片转写。
    """
    data = read_json(path)
    if isinstance(data, list):
        data = {"picks": data}
    picks = data.get("picks") or []
    hook_index = 0
    normalized, too_long = [], []
    for index, pick in enumerate(picks):
        row = dict(pick)
        source = int(row.get("src") or 1)
        if source != 1:
            raise RuntimeError(
                f"picks.json 第 {index} 条指定 src={source}；单源总入口只接受 src=1。"
                "当前女装切片任务只接受单个源视频")
        row["src"] = 1
        if not row.get("module"):
            if row.get("role") == "hook":
                row["module"] = f"hook_{chr(65 + hook_index)}"
                hook_index += 1
            else:
                row["module"] = "body"
        if row["module"] != "body" and row["module"] not in ALLOWED_HOOKS:
            raise RuntimeError(f"picks.json 第 {index} 条 module 必须是 body 或 hook_A..hook_C")
        if row.get("role") not in ALLOWED_ROLES:
            raise RuntimeError(f"picks.json 第 {index} 条 role 无效: {row.get('role')!r}")
        if not str(row.get("text") or "").strip():
            raise RuntimeError(f"picks.json 第 {index} 条缺少完整 text")
        try:
            duration = float(row["end"]) - float(row["start"])
        except Exception:
            raise RuntimeError(f"picks.json 第 {index} 条缺少可解析的 start/end")
        if float(row["start"]) < 0 or duration <= 0:
            raise RuntimeError(f"picks.json 第 {index} 条 start/end 范围无效")
        if duration < 1.2 - 1e-6:
            raise RuntimeError(f"picks.json 第 {index} 条只有 {duration:.2f}s，低于 1.2s 硬下限")
        role_limit = max_pick_seconds
        if duration > role_limit + 1e-6:
            too_long.append(f"第 {index} 条 {duration:.2f}s")
        normalized.append(row)
    if too_long:
        raise RuntimeError(
            f"picks.json 有 {len(too_long)} 条口播单元超过 {max_pick_seconds:.1f}s（"
            + "、".join(too_long[:6])
            + "）。完整口播必须控制在 1.2-5.0s；仅无法安全拆分的完整长句可放宽到 8.0s")
    if not normalized:
        raise RuntimeError("picks.json contains no picks")
    modules = {row["module"] for row in normalized}
    hooks = sorted(name for name in modules if name.startswith("hook_"))
    unknown = sorted(modules - {"body", *hooks})
    if "body" not in modules or len(hooks) > 3 or unknown:
        raise RuntimeError("picks.json requires body and allows up to 3 optional hook_* modules")
    payload = {"offsets": {"1": [0.0, str(Path(media).resolve())]}, "picks": normalized}
    if data.get("words"):
        payload["words"] = data["words"]
    write_json(output, payload)
    return hooks


def split_modules(timeline, outdir):
    rows = read_json(timeline)
    groups = {}
    for row in rows:
        groups.setdefault(row.get("module", "body"), []).append(row)
    if not groups.get("body"):
        raise RuntimeError("aligned timeline has no body segments")
    hooks = sorted(name for name in groups if name.startswith("hook_"))
    if len(hooks) > 3:
        raise RuntimeError("aligned timeline allows at most 3 hook modules")
    paths = {"body": str(Path(outdir) / "body.json")}
    write_json(paths["body"], groups["body"])
    for name in hooks:
        paths[name] = str(Path(outdir) / f"{name}.json")
        write_json(paths[name], groups[name])
    return paths


def timeline_duration(path):
    return round(sum(float(row["end"]) - float(row["start"]) for row in read_json(path)), 2)


def visual_pieces(audio, max_seconds=3.0):
    """Split synchronized picture rhythm without introducing an audio edit."""
    source = int(audio.get("src", 1))
    cursor, end = float(audio["start"]), float(audio["end"])
    pieces = []
    while cursor < end - 1e-6:
        piece_end = min(end, cursor + max_seconds)
        pieces.append({"src": source, "start": round(cursor, 3),
                       "end": round(piece_end, 3), "kind": "aroll"})
        cursor = piece_end
    return pieces


def add_render_options(command, args):
    """把渲染参数显式传给底层 renderer。

    旧实现无条件带 `--force`（产品约定要求保留旧版，
    代码和文档互相打脸），而且默认 30fps + veryfast，会把 1440x2542 的源缩到
    1080x1906 —— 与账号成片标准（1440x2560 / 源帧率 / CRF16 preset slow / 256k）
    不符。现在默认参数就是标准参数，不带 `--force`，由 resolve_output() 决定落哪个文件。
    """
    command += ["--crf", str(args.crf), "--preset", args.preset,
                "--audio-bitrate", args.audio_bitrate,
                "--loudness", str(args.loudness)]
    if args.fps:
        command += ["--fps", str(args.fps)]
    if args.width and args.height:
        command += ["--width", str(args.width), "--height", str(args.height)]
    if args.allow_upscale:
        command += ["--allow-upscale"]
    if args.force:
        command += ["--force"]
    return command


def resolve_output(path, force=False):
    """不覆盖旧成片：目标已存在时改写到 `<名字>-2.mp4`、`-3.mp4`…

    只靠 `--force` 二选一都不合适：不带它，恢复执行时因为「输出已存在」整条失败；
    带了它，每轮返工都静默覆盖上一版。改成自动分版本号，既保留旧版又能让本轮跑完；
    确实要覆盖同一路径时显式传 `--force`。
    """
    path = Path(path)
    if force or not path.exists():
        return path
    for index in range(2, 100):
        candidate = path.with_name(f"{path.stem}-{index}{path.suffix}")
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"Too many versions for {path}")


def landing_counts(workdir):
    """本段实际落地的段数/时长/被剔除原因，用于定位「为什么段数不够」。

    段数门禁是按「钩子 + 正文」并集算的，而模型写 picks 时看不到 cuts 的合并、
    去重、放宽越界和违禁词剔除，一次写 22 段可能落地只剩 17 段。这里把落地结果
    和被剔除原因一起回给模型，省掉一轮盲改。
    """
    workdir = Path(workdir)
    timeline = workdir / "timeline.json"
    if not timeline.is_file():
        return None
    rows = read_json(timeline)
    by_module = {}
    for row in rows:
        name = row.get("module", "body")
        by_module[name] = by_module.get(name, 0) + 1
    body = by_module.get("body", 0)
    hooks = {name: count for name, count in by_module.items() if name.startswith("hook_")}
    data = {"timeline_segments": len(rows), "body_segments": body, "hook_segments": hooks,
            "combination_segments": {name: body + count for name, count in hooks.items()},
            "seconds": round(sum(float(row["end"]) - float(row["start"]) for row in rows), 2)}
    manual = workdir / "manual_fix.json"
    if manual.is_file():
        try:
            fix = read_json(manual)
        except Exception:
            fix = {}
        skipped = fix.get("skipped") or []
        reasons = {}
        for item in skipped:
            key = str(item.get("why", ""))[:48]
            reasons[key] = reasons.get(key, 0) + 1
        data["skipped"] = len(skipped)
        data["skipped_reasons"] = dict(sorted(reasons.items(), key=lambda kv: -kv[1])[:5])
        data["need_manual"] = len(fix.get("need_manual") or [])
    return data


def report_issues(path, limit=10):
    """从门禁报告里抽最多 limit 条错误，返回 (清单, 错误总数)。

    原来失败摘要只有 step / returncode / log_tail，模型能看到的只有
    「failed: 2 issues」，报告在哪、哪两条错了都不知道，只能凭感觉重写 picks
    再撞一轮。现在把报告路径和 issue code 直接写进摘要。
    """
    try:
        data = read_json(path)
    except Exception:
        return [], 0
    items = []
    if isinstance(data, dict) and isinstance(data.get("combinations"), dict):
        for name, result in data["combinations"].items():
            for issue in (result.get("issues") or []):
                if issue.get("level", "error") != "error":
                    continue
                items.append({"where": name, "code": issue.get("code"),
                              "detail": issue.get("message") or issue.get("segments"),
                              "segment": issue.get("segment"),
                              "segments": issue.get("segments")})
    elif isinstance(data, dict):
        for issue in (data.get("issues") or []):
            if issue.get("level", "error") != "error":
                continue
            items.append({"segment": issue.get("segment"),
                          "segments": issue.get("segments"),
                          "code": issue.get("type") or issue.get("code"),
                          "detail": issue.get("detail") or issue.get("message")
                                    or issue.get("match") or issue.get("diagnosis")
                                    or issue.get("seconds") or issue.get("token"),
                          "actual": issue.get("actual"),
                          "expected": issue.get("expected")})
    return items[:limit], len(items)


def step_report(step):
    if step in STEP_REPORTS:
        return STEP_REPORTS[step]
    if step.startswith("validate_video_mapping_hook_"):
        label = step.removeprefix("validate_video_mapping_hook_")
        return f"dual_hook_{label}_report.json"
    if step.startswith("qc_hook_"):
        label = step.removeprefix("qc_hook_")
        return f"qc/hook_{label}/qc_report.json"
    return None


def selection_state(workdir, media, duration, limits, picks_path):
    return {
        "state": "awaiting_picks",
        "source": str(media),
        "source_seconds": round(duration, 2),
        "mode": limits["mode"],
        "read_only": [str(workdir / "candidate_digest.json"),
                      str(workdir / "overview.jpg")],
        "write": str(picks_path),
        "constraints": {
            "candidate_read_count": 1,
            "candidate_schema": {"i": "candidate id", "s": "start seconds",
                                 "e": "end seconds", "c": "category",
                                 "t": "spoken text", "r": "optional review terms"},
            "hooks": "optional; use hook_A only when the material has a genuinely stronger standalone opening",
            "body": "required common body; a body-only natural edit is valid",
            # 段数是「钩子 + 正文」并集，钩子也占额度；写多了会被 cuts 合并/剔除，
            # 写少了直接 too_few_segments 打回，所以给出可落地的区间而不是理论值。
            "segments": "target segment count is supplied by the job-specific editing constraints",
            "segment_seconds": "1.2-5.0s per complete spoken unit; an unmergeable native long "
                               "sentence may go up to 8.0s; visual shots cut shorter independently",
            "cut_effects": "cuts.py merges adjacent same-role picks (<=0.45s apart), drops "
                           "overlapping/duplicated picks and any segment hitting a banned word, "
                           "so pick 2-4 segments more than the target floor",
            "no_duplicate_text": "每个信息点全片只用一次；钩子讲过的内容正文不得重复",
            "compliance": "价格/库存/履约/催单类词按账号红线硬禁，命中整段淘汰",
            "required_pick_fields": ["src", "start", "end", "role", "module", "text"],
            "optional_pick_fields": ["manual_approved"],
            "manual_approved": "set true only after a human has listened to that segment",
            "required_top_level": ["main_product"],
        },
    }


def default_vocab_profile():
    """随包提供的账号级红线词表；不存在就只用通用词表。"""
    path = ROOT / "profiles" / "douyin-strict.json"
    return str(path) if path.is_file() else None


def resolve_vocab_profile(value):
    """解析 --vocab-profile：未给用随包配置，'none' 关掉，其余按文件路径校验。"""
    if value and str(value).lower() in {"none", "off", "no", "0"}:
        return None
    if value:
        path = Path(value).expanduser().resolve()
        if not path.is_file():
            raise RuntimeError(f"--vocab-profile 指向的文件不存在: {path}")
        return str(path)
    return default_vocab_profile()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("media")
    parser.add_argument("--workdir")
    parser.add_argument("--index-dir", help="同一源素材多次剪辑共用的转写/字幕索引目录")
    parser.add_argument("--subtitle", "--srt", dest="subtitle")
    parser.add_argument("--picks")
    parser.add_argument("--main-product")
    parser.add_argument("--allowed-product", action="append", default=[])
    parser.add_argument("--backend", choices=["auto", "mlx", "faster"], default="auto")
    parser.add_argument("--model")
    parser.add_argument("--wait-for-picks", type=int, default=0, metavar="SECONDS")
    parser.add_argument("--stage-timeout", type=int, default=7200, metavar="SECONDS",
                        help="单个 FFmpeg/ASR 阶段的最长运行时间，默认 2 小时")
    parser.add_argument("--qc", choices=["full", "technical"], default="full")
    parser.add_argument("--fps", type=float, default=None,
                        help="输出帧率；默认跟源（账号标准就是源帧率）")
    parser.add_argument("--crf", type=int, default=16)
    parser.add_argument("--preset", default="slow")
    parser.add_argument("--width", type=int, default=1440)
    parser.add_argument("--height", type=int, default=2560)
    parser.add_argument("--audio-bitrate", default="256k")
    parser.add_argument("--loudness", type=float, default=-6.5,
                        help="响度目标 LUFS（单遍 loudnorm，实测落在目标下方约 1dB）")
    parser.add_argument("--allow-upscale", action="store_true",
                        help="源小于输出尺寸时允许放大；默认不放大（源更小时会留黑边）")
    parser.add_argument("--force", action="store_true",
                        help="覆盖同名成片；默认自动分版本号，不覆盖旧版")
    parser.add_argument("--vocab-profile", default=None,
                        help="账号级合规词表配置；默认用随包的 profiles/douyin-strict.json，"
                             "传 none 则只用通用词表")
    parser.add_argument("--min-total", type=float, default=70.0, help=argparse.SUPPRESS)
    parser.add_argument("--max-total", type=float, default=120.0, help=argparse.SUPPRESS)
    parser.add_argument("--min-segments", type=int, default=18, help=argparse.SUPPRESS)
    parser.add_argument("--max-segments", type=int, default=32, help=argparse.SUPPRESS)
    parser.add_argument("--allow-manual", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--no-words", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--stop-after", choices=["align", "validate", "audit"],
                        help=argparse.SUPPRESS)
    parser.add_argument("--stop-before-render", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--original-video-only", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--skip-preflight", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--skip-overview", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()

    media = Path(args.media).resolve()
    if not media.is_file():
        raise SystemExit(f"pipeline: failed; source does not exist: {media}")
    workdir = Path(args.workdir).resolve() if args.workdir else media.parent / "_slice"
    workdir.mkdir(parents=True, exist_ok=True)
    picks_path = Path(args.picks).resolve() if args.picks else workdir / "picks.json"
    summary_path = workdir / "pipeline_summary.json"
    runner = QuietRunner(workdir / "logs", max(60, args.stage_timeout))
    vocab_profile = None
    normalized = workdir / "normalized_picks.json"
    main_product = None

    try:
        # 账号级词表配置用环境变量传递给所有子进程：prep 过滤候选、cuts 终检、
        # validate 和 audit 必须用同一份词表，否则会出现「候选已删、门禁又报」的矛盾。
        vocab_profile = resolve_vocab_profile(args.vocab_profile)
        if vocab_profile:
            os.environ[VOCAB_PROFILE_ENV] = vocab_profile
        else:
            os.environ.pop(VOCAB_PROFILE_ENV, None)

        # 恢复任务若已经有 picks，先做零成本格式检查。不要跑完整 ASR 后才发现缺字段、
        # 非法模块或超长片段。
        if picks_path.is_file():
            early_picks = read_json(picks_path)
            main_product = args.main_product or (early_picks.get("main_product")
                                                  if isinstance(early_picks, dict) else None)
            if not main_product:
                raise RuntimeError("picks.json requires top-level main_product")
            normalize_picks(picks_path, media, normalized)

        if not args.skip_preflight:
            environment = check_environment(args.backend, args.model)
            write_json(workdir / "environment.json", environment)
            if not environment["ok"]:
                raise RuntimeError("environment preflight failed; install requirements once")

        duration = media_duration(media)
        limits = adaptive_limits(duration)
        index = Path(args.index_dir).resolve() if args.index_dir else workdir / "index"
        index.mkdir(parents=True, exist_ok=True)
        prep = [sys.executable, SCRIPTS / "prep.py", media, "--workdir", index,
                "--backend", args.backend]
        # 默认让 prep 产出全片 words.json。实测 18 分钟素材：全片带词级时间戳的转写
        # 约 2 分钟，之后被 prep 的 cache_manifest 命中、改动 picks 也不再重转；
        # 而「不给词表、由 cuts.py 逐个入选窗口补转写」首次就要 3.5 分钟，改一次
        # picks 还要再付一遍。更关键的是局部窗口会稳定吐出幽灵文本（实测 60 个窗口
        # 里 15 个吐「请不吝点赞订阅…」），全片单次转写 5511 个字条里一个都没有。
        # 只有素材长到「全片转写明显不划算」时才用 --no-words 退回局部窗口。
        if args.no_words:
            prep.append("--no-words")
        if args.subtitle:
            prep += ["--subtitle", str(Path(args.subtitle).resolve())]
        if args.model:
            prep += ["--model", args.model]
        runner.run("prepare", prep)
        # 字幕模式的 words.json 由 prep.py 从时码确定性构造。禁止在此补跑
        # 等价的全片 ASR；切点与边界审计直接复用字幕边界。
        runner.run("digest", [sys.executable, SCRIPTS / "digest_candidates.py",
                              index / "candidates.json", workdir / "candidate_digest.json",
                              "--limit", str(limits["candidate_limit"]), "--compact"])
        if not args.skip_overview:
            runner.run("overview", [sys.executable, SCRIPTS / "frames.py", "overview", media,
                                    workdir / "overview.jpg", "--n",
                                    str(limits["overview_frames"])])

        if not picks_path.is_file():
            state = selection_state(workdir, media, duration, limits, picks_path)
            state["steps"] = runner.steps
            state["vocab_profile"] = vocab_profile
            write_json(summary_path, state)
            print(f"pipeline: awaiting_picks; summary={summary_path}", flush=True)
            deadline = time.monotonic() + max(0, args.wait_for_picks)
            while not picks_path.is_file() and time.monotonic() < deadline:
                time.sleep(1)
            if not picks_path.is_file():
                return

        raw_picks = read_json(picks_path)
        main_product = args.main_product or (raw_picks.get("main_product")
                                             if isinstance(raw_picks, dict) else None)
        if not main_product:
            raise RuntimeError("picks.json requires top-level main_product")
        normalize_picks(picks_path, media, normalized)
        timeline = workdir / "timeline.json"
        selected_words = workdir / "selected_words"
        cuts = [sys.executable, SCRIPTS / "cuts.py", index / "audio16k.wav", normalized,
                timeline, "--media", media, "--sentences", index / "sentences.json", "--backend", args.backend,
                "--selected-words-dir", selected_words,
                "--cache", workdir / "align_cache.json"]
        global_words = index / "words.json"
        if global_words.is_file() and not args.no_words:
            cuts += ["--words", global_words]
        if args.model:
            cuts += ["--model", args.model]
        align_key = {"source_size": media.stat().st_size,
                     "source_mtime_ns": media.stat().st_mtime_ns,
                     "picks_sha256": file_sha256(normalized),
                     # 切点算法换版本后旧 timeline 不再等价，必须重对齐。
                     # 只按 picks 判缓存会让升级后继续沿用老切点（实测踩过）。
                     "cuts_sha256": file_sha256(SCRIPTS / "cuts.py"),
                     "sentences_sha256": file_sha256(index / "sentences.json"),
                     "words_sha256": file_sha256(global_words)
                     if global_words.is_file() and not args.no_words else None,
                     "backend": args.backend, "model": args.model,
                     "no_words": args.no_words,
                     "vocab_profile_sha256": file_sha256(vocab_profile)
                     if vocab_profile else None}
        align_manifest = workdir / "alignment_manifest.json"
        cached_align = timeline.is_file() and (selected_words / "word_map.json").is_file() \
            and align_manifest.is_file() and read_json(align_manifest).get("key") == align_key
        if not cached_align:
            runner.run("align", cuts)
            write_json(align_manifest, {"key": align_key, "timeline_sha256": file_sha256(timeline)})
        if args.stop_after == "align":
            write_json(summary_path, {"state": "stopped_after_align", "source": str(media),
                                      "steps": runner.steps})
            print(f"pipeline: stopped_after_align; summary={summary_path}")
            return

        timelines = split_modules(timeline, workdir / "timelines")
        validation = workdir / "validation.json"
        validate = [sys.executable, SCRIPTS / "validate_timeline.py", "--body",
                    timelines["body"]]
        for name, path in sorted(timelines.items()):
            if name.startswith("hook_"):
                validate += ["--hook", f"{name.removeprefix('hook_')}={path}"]
        validate += ["--src", f"1={media}", "--min-total", str(args.min_total),
                     "--max-total", str(args.max_total), "--min-segments",
                     str(args.min_segments), "--max-segments", str(args.max_segments),
                     "--require-structure", "--report", validation]
        runner.run("validate", validate)
        if args.stop_after == "validate":
            write_json(summary_path, {"state": "stopped_after_validate", "source": str(media),
                                      "steps": runner.steps})
            print(f"pipeline: stopped_after_validate; summary={summary_path}")
            return
        audit = [sys.executable, SCRIPTS / "audit_bounds.py", timeline,
                 "--word-map-json", selected_words / "word_map.json",
                 "--main-product", main_product, "--require-binding",
                 "--report", workdir / "bounds_report.json"]
        for product in args.allowed_product:
            audit += ["--allowed-product", product]
        if args.allow_manual:
            audit.append("--allow-manual")
        runner.run("audit", audit)
        if args.stop_after == "audit":
            write_json(summary_path, {"state": "stopped_after_audit", "source": str(media),
                                      "steps": runner.steps})
            print(f"pipeline: stopped_after_audit; summary={summary_path}")
            return
        # This 16 kHz / 32 kbps preview is generated before any HD video encode.  Its
        # report replays the locked source ASR at every head, tail, and concatenation
        # point; a failure returns to automatic candidate repair instead of waiting for
        # a human ``need_manual`` decision.
        runner.run("audio_acceptance", [
            sys.executable, SCRIPTS / "audio_acceptance.py", timeline, media,
            selected_words / "word_map.json",
            "--preview", workdir / "audio_preview.m4a",
            "--report", workdir / "audio_acceptance.json",
            "--backend", args.backend,
            *(["--model", args.model] if args.model else []),
        ])
        dual_dir = workdir / "dual_timelines"
        dual_dir.mkdir(parents=True, exist_ok=True)
        dual_timelines = {}
        for name, path in sorted(timelines.items()):
            section = "hook" if name.startswith("hook_") else "body"
            dual_rows = [{
                "section": section,
                "audio": row,
                "video": visual_pieces(row),
            } for row in read_json(path)]
            target = dual_dir / f"{name}.json"
            write_json(target, dual_rows)
            dual_timelines[name] = str(target.resolve())
        write_json(workdir / "video_mapping_report.json", {
            "ok": True, "mode": "original_video", "dual_timelines": dual_timelines,
        })
        deliverables = workdir / "deliverables"
        deliverables.mkdir(parents=True, exist_ok=True)
        runner.run("validate_video_mapping_body", [sys.executable,
                   SCRIPTS / "validate_dual_timeline.py", dual_timelines["body"],
                   "--src", f"1={media}", "--min-total", "0",
                   "--max-total", str(args.max_total),
                   "--report", workdir / "dual_body_report.json"])
        for name, path in sorted(timelines.items()):
            if not name.startswith("hook_"):
                continue
            label = name.removeprefix("hook_")
            runner.run(f"validate_video_mapping_hook_{label}", [sys.executable,
                       SCRIPTS / "validate_dual_timeline.py", dual_timelines[name],
                       "--src", f"1={media}", "--min-total", "0", "--max-total", "10",
                       "--report", workdir / f"dual_hook_{label}_report.json"])
        if args.stop_before_render:
            summary = {
                "state": "ready_to_render", "publish_ready": False,
                "source": str(media), "mode": limits["mode"],
                "segments": landing_counts(workdir),
                "audio_acceptance": str((workdir / "audio_acceptance.json").resolve()),
                "audio_preview": str((workdir / "audio_preview.m4a").resolve()),
                "dual_timelines": dual_timelines,
                "steps": runner.steps,
            }
            write_json(summary_path, summary)
            print(f"pipeline: ready_to_render; summary={summary_path}")
            return
        body_key = render_key(dual_timelines["body"], media, args)
        body_manifest = workdir / "stage_manifests" / "render_body.json"
        body_video = None if args.force else cached_artifact(body_manifest, body_key)
        if body_video is None:
            body_video = resolve_output(deliverables / "body.mp4", args.force)
            runner.run("render_body", add_render_options(
                [sys.executable, SCRIPTS / "render_dual.py", dual_timelines["body"], body_video,
                 "--src", f"1={media}"], args))
            save_artifact(body_manifest, body_key, body_video)
        else:
            runner.steps.append({"name": "render_body", "seconds": 0.0, "ok": True,
                                 "cached": True})
        hook_videos = {}
        for name, path in sorted(timelines.items()):
            if not name.startswith("hook_"):
                continue
            label = name.removeprefix("hook_")
            hook_key = render_key(dual_timelines[name], media, args)
            hook_manifest = workdir / "stage_manifests" / f"render_hook_{label}.json"
            output = None if args.force else cached_artifact(hook_manifest, hook_key)
            if output is None:
                output = resolve_output(deliverables / f"hook_{label}.mp4", args.force)
                runner.run(f"render_hook_{label}", add_render_options(
                    [sys.executable, SCRIPTS / "render_dual.py", dual_timelines[name], output,
                     "--src", f"1={media}"], args))
                save_artifact(hook_manifest, hook_key, output)
            else:
                runner.steps.append({"name": f"render_hook_{label}", "seconds": 0.0,
                                     "ok": True, "cached": True})
            hook_videos[label] = output

        preview_cmd = [sys.executable, SCRIPTS / "assemble_hooks.py", body_video,
                       deliverables / "previews", "--force"]
        for label, path in sorted(hook_videos.items()):
            preview_cmd += ["--hook", f"{label}={path}"]
        preview_key = {"version": 1, "body": lightweight_identity(body_video),
                       "hooks": {label: lightweight_identity(path)
                                 for label, path in sorted(hook_videos.items())},
                       "script_sha256": file_sha256(SCRIPTS / "assemble_hooks.py")}
        preview_manifest = workdir / "stage_manifests" / "previews.json"
        preview_output = None if args.force else cached_artifact(preview_manifest, preview_key)
        hook_manifest_path = deliverables / "previews" / "hook_manifest.json"
        if preview_output is None or not hook_manifest_path.is_file():
            runner.run("previews", preview_cmd)
            save_artifact(preview_manifest, preview_key, hook_manifest_path)
        else:
            runner.steps.append({"name": "previews", "seconds": 0.0, "ok": True,
                                 "cached": True})

        qc_cmd = [sys.executable, SCRIPTS / "qc.py", body_video, workdir / "qc" / "body",
                  "--expected-width", str(args.width), "--expected-height", str(args.height),
                  "--technical-only"]
        runner.run("qc_body", qc_cmd)
        for label, video in sorted(hook_videos.items()):
            runner.run(f"qc_hook_{label}", [sys.executable, SCRIPTS / "qc.py", video,
                                             workdir / "qc" / f"hook_{label}",
                                             "--expected-width", str(args.width),
                                             "--expected-height", str(args.height),
                                             "--technical-only"])

        body_seconds = timeline_duration(timelines["body"])
        combinations = {label: round(body_seconds + timeline_duration(
            timelines[f"hook_{label}"]), 2) for label in hook_videos}
        summary = {
            "state": "complete", "publish_ready": True,
            "source": str(media), "mode": limits["mode"],
            "body_seconds": body_seconds, "combinations": combinations,
            "segments": landing_counts(workdir),
            "vocab_profile": vocab_profile,
            "render": {"width": args.width, "height": args.height,
                       "fps": args.fps or "source", "crf": args.crf,
                       "preset": args.preset, "audio_bitrate": args.audio_bitrate,
                       "loudness_target": args.loudness},
            "deliverables": {"body": str(body_video),
                             "hooks": {k: str(v) for k, v in hook_videos.items()},
                             "previews": str(deliverables / "previews")},
            "steps": runner.steps,
        }
        write_json(summary_path, summary)
        print(f"pipeline: complete; hooks={len(hook_videos)}; body={body_seconds:.2f}s; "
              f"summary={summary_path}")
    except Exception as exc:
        # 失败摘要必须能让人直接定位：报告路径 + issue code + 实际落地段数。
        # 只给 step / returncode / log_tail 的话，模型只能凭感觉重写 picks 再撞一轮
        # （原来就只能看到 "failed: 2 issues"，报告在哪、错在哪都不知道）。
        issue = {"state": "failed", "error": str(exc), "steps": runner.steps,
                 "vocab_profile": vocab_profile}
        landing = landing_counts(workdir)
        if landing:
            issue["segments"] = landing
        if isinstance(exc, PipelineFailure):
            issue.update({"step": exc.step, "returncode": exc.returncode,
                          "log": exc.log, "log_tail": tail(Path(exc.log).read_text(
                              encoding="utf-8", errors="replace"))})
            report = step_report(exc.step)
            report_path = workdir / report if report else None
            if report_path is not None and report_path.is_file():
                items, total = report_issues(report_path)
                issue["report"] = str(report_path)
                issue["issue_count"] = total
                if items:
                    issue["issues"] = items
        write_json(summary_path, issue)
        print(f"pipeline: failed; step={issue.get('step', 'setup')}; summary={summary_path}")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
