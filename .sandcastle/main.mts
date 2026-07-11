// Sequential Reviewer — implement-then-review loop
//
// This template drives a two-phase workflow per issue:
//   Phase 1 (Implement): A sonnet agent picks an open issue, works on it
//                        on the shared branch `sandcastle/dev`, commits the
//                        changes, and signals completion.
//   Phase 2 (Review):    A second sonnet agent reviews the latest commit on
//                        that branch and either approves it or makes
//                        corrections directly on the branch.
//
// Both phases share a single sandbox created via createSandbox(), so the
// implementer and reviewer work on the same shared branch.
//
// The outer loop repeats up to MAX_ITERATIONS times, processing one issue per
// iteration and stopping early once the backlog is exhausted (an implement
// phase that produces no commits). This is a middle-complexity option between
// the simple-loop (no review gate) and the parallel-planner (concurrent
// execution with a planning phase).
//
// Usage:
//   npx tsx .sandcastle/main.mts
// Or add to package.json:
//   "scripts": { "sandcastle": "npx tsx .sandcastle/main.mts" }

import * as sandcastle from "@ai-hero/sandcastle";
import { docker } from "@ai-hero/sandcastle/sandboxes/docker";
import { noSandbox } from "@ai-hero/sandcastle/sandboxes/no-sandbox";

// ---------------------------------------------------------------------------
// Configuration
// ---------------------------------------------------------------------------

// Maximum number of implement→review cycles to run before stopping.
// Each cycle works on one issue. Raise this to process more issues per run.
const MAX_ITERATIONS = 50;

// Hooks run inside the sandbox before the agent starts each iteration.
// Install the worktree's Python package (editable) + dev extras into the
// active conda environment so the agent can import hypoargus and run pytest
// from the worktree without extra setup.
const hooks = {
  sandbox: { onSandboxReady: [{ command: "python -m pip install -e '.[dev]'" }] },
};

// Nothing to copy from the host for a Python project — the conda env on the
// host already has the interpreter and deps; the hook above installs the
// worktree package into it.
const copyToWorktree: string[] = [];

// ---------------------------------------------------------------------------
// Main loop
// ---------------------------------------------------------------------------

for (let iteration = 1; iteration <= MAX_ITERATIONS; iteration++) {
  console.log(`\n=== Iteration ${iteration}/${MAX_ITERATIONS} ===\n`);

  // All development accumulates on ONE shared long-lived branch so the work is
  // not scattered across per-issue branches (which is what made issue status
  // diverge from main). createSandbox closes only remove the worktree, never the
  // branch, so each iteration re-checks out the same `sandcastle/dev` and stacks
  // its commit on top of the previous one. A human reviews the whole branch and
  // merges to main once the backlog is drained.
  const branch = "sandcastle/dev";

  // Create a single sandbox that both the implementer and reviewer share.
  // This gives both agents a real, named branch that persists across phases.
  const sandbox = await sandcastle.createSandbox({
    branch,
    sandbox: noSandbox(),
    hooks,
    copyToWorktree,
  });

  try {
    // -----------------------------------------------------------------------
    // Phase 1: Implement
    //
    // A sonnet agent picks the next open issue, writes the
    // implementation (using TDD: Red → Green → Refactor with pytest), and
    // commits the result on `sandcastle/dev`.
    //
    // The agent signals completion via <promise>COMPLETE</promise> when done.
    // -----------------------------------------------------------------------
    // One iteration per outer pass: implement a single issue, commit it on the
    // shared branch, then hand the latest commit to the reviewer. The next
    // iteration stacks the next issue's commit on top of the same branch.
    // Retry a failed implement attempt up to MAX_IMPLEMENT_RETRIES times.
    //
    // Failure = the agent neither committed nor emitted the completion signal
    // (it stalled in plan mode, hit maxIterations, or crashed). This is
    // distinct from the backlog being drained, which the agent signals
    // explicitly via <promise>COMPLETE</promise>. Without this retry, a single
    // plan-mode stall reads as "backlog empty" and stops the whole run.
    const MAX_IMPLEMENT_RETRIES = 3;
    let implement: Awaited<ReturnType<typeof sandbox.run>> | undefined;
    let backlogDrained = false;
    for (let attempt = 1; attempt <= MAX_IMPLEMENT_RETRIES; attempt++) {
      implement = await sandbox.run({
        name: "implementer",
        maxIterations: 1,
        agent: sandcastle.claudeCode("glm-5.2[1m]"),
        promptFile: "./.sandcastle/implement-prompt.md",
      });

      // The agent declared the backlog exhausted (or all remaining issues
      // blocked) — stop the whole run, regardless of commits this pass.
      if (implement.completionSignal) {
        console.log(`Implementation agent signaled completion: ${implement.completionSignal}. Stopping.`);
        backlogDrained = true;
        break;
      }
      // One issue implemented and committed — hand off to review.
      if (implement.commits.length > 0) {
        break;
      }
      // Neither: this attempt failed. Retry if attempts remain.
      console.log(
        `Implementation attempt ${attempt}/${MAX_IMPLEMENT_RETRIES} produced no commit and no completion signal — ` +
          `${attempt < MAX_IMPLEMENT_RETRIES ? "retrying" : "giving up"}.`
      );
    }

    if (backlogDrained) {
      break; // outer loop
    }

    if (!implement || implement.commits.length === 0) {
      // All retries failed without producing work — stop rather than spin on
      // a broken iteration. A human should inspect the log file.
      console.log("Implementation failed after retries. Stopping.");
      break;
    }

    console.log(`\nImplementation complete on branch: ${branch}`);
    console.log(`Commits: ${implement.commits.length}`);

    // -----------------------------------------------------------------------
    // Phase 2: Review
    //
    // A second sonnet agent reviews the latest commit produced by Phase 1
    // (via `git show HEAD`) and either approves it or makes corrections
    // directly on the shared branch.
    // -----------------------------------------------------------------------
    await sandbox.run({
      name: "reviewer",
      maxIterations: 1,
      agent: sandcastle.claudeCode("glm-5.2[1m]"),
      promptFile: "./.sandcastle/review-prompt.md",
    });

    console.log("\nReview complete.");
  } finally {
    await sandbox.close();
  }
}

console.log("\nAll done.");
