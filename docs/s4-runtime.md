S4 now annotates candidates in batches of 40, with three to six concurrent model calls.
WorkBuddy uses batches of 20 for uncached work; existing valid 40-row cache entries
remain reusable. A worker failure stops queued requests before they start additional
model calls. Already running requests are collected under their time limits.
Three neighboring candidates on each side provide context for dependencies crossing batch boundaries.
Completed batches are cached under the job workspace's `s4-cache/`, keyed by candidate
content, context, model, provider setting and annotation version. Invalid cache entries are
recomputed. A failed request preserves other completed batches; a changed duration target
does not invalidate annotations. Changed candidate content or model does.

After annotation, a global fact-key reconciliation merges equivalent facts across batches.
Complementary facts and conflicting values must remain distinct. The program validates
complete, unique key coverage before accepting the mapping. Composition receives one
shortest complete representative per identical topic/fact-set, including all dependencies.
Existing topic, dependency, duration and fact-duplication validation remains in force.

S4 has no shared stage budget, annotation time allocation or reserved composition time.
Each model request independently receives the caller's configured AI timeout (normally
900 seconds). Earlier calls do not shorten later calls. `PIPELINE_S4_BUDGET` is ignored.
Elapsed time does not skip candidate batches or trigger annotation degradation. Actual
request failures still stop queued work and preserve successful caches; running provider
calls are collected under their individual timeouts. S5's three-review limit is unchanged.

Batch completion, cache reuse, fact reconciliation and composition appear as progress
events without marking the stage complete. Prompts request direct JSON and no tool use;
the current Antigravity CLI does not expose a tool-disable switch, so this is not a hard
tool restriction. The configured individual request timeout still applies.

There is no maximum duration for the whole S4 stage. No automatic provider/model
switching is performed.
