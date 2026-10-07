"""Bound S4 work, persist completed batches and reconcile facts across batches."""
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import math
import os
from pathlib import Path
import time
from threading import Event

from .editorial import inventory, digest, save, obj, array, STR
from .errors import AIReturnError

VERSION = 1
FACT_SCHEMA = obj({'groups': array(obj({'keys': array(STR)}))})
CALL_TIMEOUT = 90
MAX_WORKERS = 6
# Annotation and composition share one budget; the composition call still needs room.
ANNOTATION_SHARE = 0.6


class OrderRuntime:
    def __init__(self, workdir, model, timeout, call, progress=None):
        self.root = Path(workdir) / 's4-cache'
        self.root.mkdir(parents=True, exist_ok=True)
        self.model, self.call, self.progress = model, call, progress or (lambda _: None)
        self.started = time.monotonic()
        self.budget = min(timeout, 300)
        self.deadline = self.started + self.budget
        self.annotation_deadline = self.started + self.budget * ANNOTATION_SHARE
        self.identity = {'version': VERSION, 'model': model,
                         'provider': os.environ.get('PIPELINE_AI_PROVIDER', 'auto')}

    def invoke(self, model, prompt, schema, timeout, *, deadline=None):
        remaining = int((deadline or self.deadline) - time.monotonic())
        if remaining < 1:
            raise AIReturnError(f'S4 超过总时间预算 {self.budget} 秒；已完成标注已缓存')
        return self.call(model, prompt, schema, min(timeout, remaining, CALL_TIMEOUT))

    def _load(self, key):
        try:
            return json.loads((self.root / (key + '.json')).read_text(encoding='utf8'))
        except (OSError, ValueError):
            return None

    def labels(self, candidates):
        whole_key = digest(dict(self.identity, candidates=candidates))
        cached = self._load(whole_key)
        if cached is not None:
            try:
                result = inventory(candidates, self.model, 90, lambda *args: cached)
                self.progress(f'S4 复用全部 {len(result)} 段语义标注')
                return result
            except AIReturnError:
                pass
        # Neighbor overlap preserves dependencies crossing a batch boundary.
        batches = []
        batch_size = 20 if self.identity['provider'] == 'workbuddy' else 40
        for offset in range(0, len(candidates), 40):
            rows = candidates[offset:offset + 40]
            context = candidates[max(0, offset - 3):offset] + candidates[offset + 40:offset + 43]
            key = digest(dict(self.identity, rows=rows, context=context))
            # Keep existing validated 40-row cache hits, split only uncached WorkBuddy work.
            value = self._load(key)
            valid = False
            if value is not None:
                try:
                    inventory(rows, self.model, 90, lambda *args: value, context=context)
                    valid = True
                except AIReturnError:
                    pass
            if valid or batch_size == 40:
                batches.append((rows, context, key))
            else:
                for start in range(offset, min(offset + 40, len(candidates)), batch_size):
                    subset = candidates[start:start + batch_size]
                    neighbors = candidates[max(0, start - 3):start] + candidates[start + batch_size:start + batch_size + 3]
                    subkey = digest(dict(self.identity, rows=subset, context=neighbors))
                    batches.append((subset, neighbors, subkey))
        result = {}
        stopped = Event()
        failures = []
        def annotate(batch):
            # A batch that cannot finish in the annotation budget is dropped, not fatal:
            # successful batches stay cached and the next rerun covers the gap.
            limit = min(self.annotation_deadline, self.deadline)
            if stopped.is_set() or time.monotonic() >= limit:
                return {}, False
            rows, context, key = batch
            value = self._load(key)
            if value is not None:
                try:
                    return inventory(rows, self.model, CALL_TIMEOUT, lambda *args: value,
                                     context=context), True
                except AIReturnError:
                    pass
            try:
                labels = inventory(rows, self.model, CALL_TIMEOUT,
                                   lambda *args: self.invoke(*args, deadline=limit),
                                   context=context)
            except Exception as exc:
                # Signal before this worker can be reused for another queued request.
                failures.append(exc)
                stopped.set()
                return {}, False
            save(self.root / (key + '.json'), {'candidates': list(labels.values())})
            return labels, False
        # Spread the batches over as many waves as the annotation budget allows.
        waves = max(1, int(self.budget * ANNOTATION_SHARE) // CALL_TIMEOUT)
        workers = min(MAX_WORKERS, max(3, math.ceil(len(batches) / waves)))
        self.progress(f'S4 语义标注：{len(candidates)} 段 / {len(batches)} 批 / 并发 {workers}')
        # Provider calls honor the remaining shared deadline; collect all workers before returning.
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = [executor.submit(annotate, batch) for batch in batches]
            try:
                for done, future in enumerate(as_completed(futures), 1):
                    labels, hit = future.result()
                    result.update(labels)
                    self.progress(f'S4 语义标注 {done}/{len(batches)} 批完成' + ('（缓存）' if hit else ''))
            except Exception:
                for future in futures:
                    future.cancel()
                raise
        if not result:
            # Without any label there is nothing to compose from; surface the real cause.
            raise failures[0] if failures else AIReturnError(
                f'S4 超过总时间预算 {self.budget} 秒；未完成任何语义标注')
        missing = len(candidates) - len(result)
        if missing:
            self.progress(f'S4 语义标注降级：{missing}/{len(candidates)} 段本轮未标注，'
                          '仅使用已标注候选，未完成批次将在重跑时复用缓存补齐')
        # Local batch keys alone cannot ensure equivalent facts share a key globally.
        keys = sorted({fact for label in result.values() for fact in label['facts']})
        if len(batches) > 1 and keys:
            self.progress('S4 全局事实合并：消除跨批次重复语义')
            prompt = ('将以下事实键中语义完全相同的分组，互补事实、不同对象和冲突数值不能合并。'
                      '每个输入键恰好出现一次，独立事实为单元素组。直接返回JSON，不写脚本、不调用工具。\n'
                      + json.dumps(keys, ensure_ascii=False))
            data = self.invoke(self.model, prompt, FACT_SCHEMA, 90)
            groups = data.get('groups')
            seen, mapping = set(), {}
            if not isinstance(groups, list):
                raise AIReturnError('S4 全局事实合并缺少 groups')
            for group in groups:
                members = group.get('keys') if isinstance(group, dict) else None
                if (not isinstance(members, list) or not members
                        or any(not isinstance(k, str) or k not in keys or k in seen for k in members)
                        or len(set(members)) != len(members)):
                    raise AIReturnError('S4 全局事实合并键重复或越界')
                seen.update(members)
                mapping.update({k: min(members) for k in members})
            if seen != set(keys):
                raise AIReturnError('S4 全局事实合并遗漏事实')
            for label in result.values():
                label['facts'] = sorted({mapping[k] for k in label['facts']})
        if not missing:
            save(self.root / (whole_key + '.json'), {'candidates': list(result.values())})
        return result
