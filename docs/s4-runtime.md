S4 now annotates candidates in batches of 40, with up to three concurrent model calls.
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

The S4 budget is the smaller of the caller's timeout and 300 seconds. Each model call
receives at most 90 seconds, further capped by the shared remaining budget. Pending work
is cancelled on failure; running provider calls are collected and terminated by their own
timeouts. Process cleanup can take a few additional seconds. Budget exhaustion is an
explicit failure, not approval of an unchecked script. S5's three-review limit is unchanged.

Batch completion, cache reuse, fact reconciliation and composition appear as progress
events without marking the stage complete. Prompts request direct JSON and no tool use;
the current Antigravity CLI does not expose a tool-disable switch, so this is not a hard
tool restriction. Time budgets apply regardless of the agent's tool behavior.

These budgets limit waiting; they do not guarantee model availability or a particular
completion time. No automatic provider/model switching is performed.
