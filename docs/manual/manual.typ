#set document(
  title: "PatchPilot technical manual",
  author: "PatchPilot",
  description: "Architecture, submodel composition, workflow, and current limitations of PatchPilot 0.1.0.",
)
#set page(
  paper: "a4",
  margin: (top: 16mm, bottom: 18mm, x: 16mm),
  header: context {
    if counter(page).get().first() > 0 {
      set text(font: "Liberation Sans", size: 8pt, fill: rgb("#5c6b7a"))
      [PatchPilot technical manual]
      h(1fr)
      [Local-first maintenance]
      v(-2pt)
      line(length: 100%, stroke: 0.4pt + rgb("#d5dde4"))
    }
  },
  footer: context {
    set text(font: "Liberation Sans", size: 8pt, fill: rgb("#5c6b7a"))
    line(length: 100%, stroke: 0.4pt + rgb("#d5dde4"))
    v(3pt)
    grid(
      columns: (1fr, auto, 1fr),
      align(left)[Version 0.1.0],
      align(center)[#counter(page).display()],
      align(right)[16 November 2026],
    )
  },
)
#set text(font: "Liberation Serif", size: 10.5pt, lang: "en", hyphenate: true)
#set par(leading: 0.62em, spacing: 0.72em, justify: false)
#set heading(numbering: "1.1")
#show heading: set text(font: "Liberation Sans")
#show heading.where(level: 1): it => {
  let numbered = it.numbering != none
  block(breakable: false, above: 1.3em, below: 10pt)[
    #text(font: "Liberation Sans", size: 17pt, weight: "bold", fill: rgb("#1c2430"))[
      #if numbered [
        #counter(heading).display()
        #h(0.45em)
      ]
      #it.body
    ]
    #v(2pt)
    #line(length: 100%, stroke: 0.8pt + rgb("#1c2430"))
  ]
}
#show heading.where(level: 2): it => block(breakable: false, above: 1.15em, below: 0.55em)[
  #text(font: "Liberation Sans", size: 12.5pt, weight: "bold", fill: rgb("#1c2430"))[
    #counter(heading).display()
    #h(0.4em)
    #it.body
  ]
]
#show heading.where(level: 3): it => block(breakable: false, above: 0.9em, below: 0.35em)[
  #text(font: "Liberation Sans", size: 11pt, weight: "bold", fill: rgb("#243140"))[
    #counter(heading).display()
    #h(0.35em)
    #it.body
  ]
]
#show raw.where(block: false): it => {
  set text(font: "Liberation Mono", size: 8pt)
  let chars = it.text.clusters()
  chars.enumerate().map(item => {
    let i = item.at(0)
    let c = item.at(1)
    let prev = if i > 0 { chars.at(i - 1) } else { "" }
    let upper = c.len() == 1 and c >= "A" and c <= "Z"
    let prev-lower = prev.len() == 1 and prev >= "a" and prev <= "z"
    let punct = c in ("_", "/", ".", "-", ":", ",")
    if punct or (upper and prev-lower) { "\u{200B}" + c } else { c }
  }).join()
}
#set figure(supplement: [Figure])
#show raw.where(block: true): set text(font: "Liberation Mono", size: 8pt)
#set table(stroke: 0.4pt + rgb("#c5cdd6"), inset: (x: 4pt, y: 3.5pt))
#show table: set text(size: 9pt)

#let ink = rgb("#1c2430")
#let muted = rgb("#5c6b7a")
#let linec = rgb("#c5cdd6")
#let classify = rgb("#e7eef6")
#let codefill = rgb("#e7f2ea")
#let gatefill = rgb("#f6eee6")
#let storefill = rgb("#f3f0ea")
#let warnfill = rgb("#fbf6ee")
#let warnbar = rgb("#8a5a12")

#let note(body) = block(
  width: 100%,
  breakable: true,
  inset: (left: 10pt, right: 9pt, y: 8pt),
  fill: warnfill,
  stroke: (left: 2.6pt + warnbar, rest: 0.4pt + rgb("#e4d3b5")),
  radius: (right: 2pt),
)[
  #text(font: "Liberation Sans", size: 8pt, weight: "bold", fill: warnbar, tracking: 0.08em)[NOTEWORTHY]
  #v(3pt)
  #body
]

#let card(body, fill: white, width: 100%) = box(
  width: width,
  inset: 6pt,
  radius: 2.5pt,
  fill: fill,
  stroke: 0.6pt + rgb("#3d4c5c"),
)[#set par(leading: 0.45em); #body]

#let down-arrow() = align(center)[
  #v(1pt)
  #text(fill: rgb("#3d4c5c"), size: 11pt)[↓]
  #v(1pt)
]

#let flow(steps) = {
  for (i, step) in steps.enumerate() {
    block(width: 100%, inset: 6pt, radius: 2.5pt, fill: white, stroke: 0.6pt + rgb("#3d4c5c"))[#step]
    if i + 1 < steps.len() { down-arrow() }
  }
}

#page(header: none, footer: none, margin: (x: 20mm, y: 22mm))[
  #set text(hyphenate: false)
  #v(28mm)
  #line(length: 28mm, stroke: 1.6pt + ink)
  #v(6mm)
  #text(font: "Liberation Sans", size: 11pt, fill: muted, tracking: 0.14em)[TECHNICAL MANUAL]
  #v(4mm)
  #text(font: "Liberation Sans", size: 34pt, weight: "bold", fill: ink)[PatchPilot]
  #v(3mm)
  #text(size: 13pt, fill: rgb("#243140"))[
    Local-first, human-supervised software maintenance.
  ]
  #v(8mm)
  #text(size: 11pt)[
    How the processes, agents, and models are composed; what each stage is allowed to do; the problems the implementation had to solve; and the limits that are still in force.
  ]
  #v(1fr)
  #line(length: 100%, stroke: 0.6pt + linec)
  #v(3mm)
  #grid(
    columns: (1fr, 1fr),
    gutter: 8mm,
    text(size: 10pt)[
      *Version* 0.1.0 \
      *Date* 16 November 2026 \
      *Source of truth* the Python package in `src/patchpilot`
    ],
    text(size: 10pt)[
      This manual follows the code where the shorter notes in `README.md` and `docs/ARCHITECTURE.md` have fallen behind, in particular the baseline test that now runs before a patch is written.
    ],
  )
]

#counter(page).update(1)
#show outline: it => {
  set heading(numbering: none)
  it
}
#outline(title: [Contents], depth: 2, indent: 1.1em)
#pagebreak()

= What PatchPilot is

PatchPilot takes a bug report, investigates a registered checkout, proposes a plan, and waits. A person approves the plan before any edit. The edit is applied in a private Git worktree. Pytest runs in a separate process that has no database password and no model credentials. A second person, or the same person, must approve again before a task branch can be pushed. Nothing in the loop merges a pull request or deploys.

The HTTP API does not call a model. It inserts a queued task and later reads what the worker stored. A separate worker claims one task at a time and runs a LangGraph workflow. Every specialist node returns to a supervisor. The supervisor's route is a deterministic rule. A model may recommend a route only when that rule has already listed it.

The model is treated as untrusted. So is the text of a bug report, and so is the text inside a registered repository, including comments that tell the model to ignore its instructions. The person who starts Compose is the administrator. The API listens on localhost and has no authentication.

Two small FastAPI projects live under `examples/` for a local demonstration. `demo_service` returns a discount for a known customer tier and raises `KeyError` for an unknown one, so the endpoint answers HTTP 500. The intended response is HTTP 422. `minitask` is a small task board. Registration is a local command. A task cannot send a filesystem path or a Git URL.

= How the processes fit together

Four processes share one PostgreSQL database and a set of volumes. Credentials are split on purpose. The API can create tasks and record decisions. It cannot call Ollama, and it is not given a GitHub token. The worker is the only process that receives model settings and, when publication is enabled, the GitHub token. The runner exits if its environment already contains a secret. Pytest then inherits a second, shorter allowlist, so a proxy variable set on the runner is not visible to the suite.

#figure(
  image("diagrams/system.svg", width: 100%),
  caption: [Processes and mounts. The worker is the only process with model settings and, when publication is on, the GitHub token. The dashed GitHub box stays unused until that second approval. The runner receives a job id and nothing else.],
)

#v(2mm)

#table(
  columns: (1.15fr, 1.3fr, 1.5fr),
  table.header([*Volume or mount*], [*Seen by*], [*What is written*]),
  [`pgdata`], [API, worker], [Business rows and LangGraph checkpoints. The runner has no database URL.],
  [`artifacts`], [API, worker], [Plan, proposal, applied diff, test report, and review JSON. The API reads them and does not show filesystem paths.],
  [`workspaces`], [Worker], [A bare clone and a worktree per task. The API does not receive this mount.],
  [Runner volume], [Worker writes the request. Runner writes the result.], [A copy of one worktree, a job file that contains only a job id, and a result file.],
  [`REPOSITORY_ROOT`], [Worker, read-only], [Registered checkouts. Compose mounts the host directory on `/repositories` with `:ro,Z`.],
)

Named volumes survive `docker compose down`. `docker compose down -v` deletes them. Each service log rotates at 10 MB and keeps three files. Process log lines are truncated at 500 characters, and a PostgreSQL password, a bearer token, or an `sk-` key in a line is replaced before the line is written.

#note[
  The runner volume is one tree for every job. That is weaker isolation than a fresh mount per task, so worker concurrency is fixed at 1 and cannot be raised. A second job is not staged while the first is still in that tree. The same cap appears again as a Postgres advisory lock around local Ollama calls, so two workers cannot occupy the GPU model at once even if someone later tried to raise the poll concurrency.
]

== Where a fact is allowed to live

The dashboard and the API read business rows. They do not read checkpoint blobs. Those blobs can contain model text, file text that was in a prompt, and the applied diff. The public records are smaller on purpose.

#table(
  columns: (1.2fr, 1.4fr, 1.5fr),
  table.header([*Fact*], [*Stored as*], [*Kept out of the public record*]),
  [Task status, stage, summary], [`tasks`], [Host paths, diff text, file text.],
  [Attempt, lease, worker id], [`task_runs`], [The lease renewal thread's connection string.],
  [Audit trail], [`events`], [The idempotency key. Secret-like payload fields become `[redacted]`.],
  [Plan, proposal, diff, tests, review], [Artifact file plus an `artifacts` row: relative path, sha256, size, version.], [The absolute path. `result_json` keeps checksums and paths inside the repo, not the diff bytes.],
  [Graph thread], [`checkpoints`, `checkpoint_blobs`, `checkpoint_writes`, `checkpoint_migrations`], [Not served by the API. Not migrated by Alembic.],
  [Clone and worktree], [`workspaces` plus the workspace volume], [`result_json` stores the base commit and the branch, not a host path.],
  [Model usage], [`model_calls`], [The prompt and the model text. Cost stays unknown unless a local price file marks that exact model verified.],
  [Draft pull request], [`publications`], [The token. The client has no merge method.],
)

= Composition of the submodels

"Submodel" here means one stage's structured call: a provider, a model name, a JSON schema, a prompt assembled from earlier stages, and a deterministic checker that runs after the model returns. Six stages can call a model. Several other nodes do not. The checker, not the model, decides whether the object's contents may affect the repository.

== The six calls

#figure(
  image("diagrams/submodels.svg", width: 100%),
  caption: [Default composition of the six model stages. Classification stages share one client. Code stages share another. Under each stage is the schema it must return, then the checker that runs after validation. A stage set to `fake` or `openai` does not use these Ollama tags.],
)

`LLM_STAGE_MODELS` overrides any stage with `stage=provider:model`. An empty value uses `LLM_PROVIDER` and `LLM_MODEL` for all six. The worker default is the split above. Pytest and CI set `LLM_PROVIDER=fake` and leave `LLM_STAGE_MODELS` empty, so those runs stay offline. `scripts/bootstrap.sh` asks `python -m patchpilot.llm.required_models` for the distinct Ollama tags and pulls each one. Adding a tag does not require a new install script.

Providers are built once per `(provider, model)` pair. Triage and plan therefore share one Ollama client when they name the same tag. A cloud client is constructed only for a stage that selects `openai`. Building a router that still uses `fake` or Ollama does not open a cloud connection, and it does not put the API key in a URL.

#table(
  columns: (0.9fr, 1.15fr, 1.7fr, 1.35fr),
  table.header([*Stage*], [*Schema returned*], [*What the call is given*], [*What happens after the schema validates*]),
  [`triage`], [`TriageResult`], [Title and description only. No repository text.], [Stored on the task. A missing repository ends the workflow here.],
  [`investigate`], [`InvestigatorStep`, then `InvestigationReport`], [Triage, a seeded tree and search, and the tool observations so far. File text is in the prompt for this call and is not written to the task.], [Citations whose lines were not returned by `read_text_file` are dropped. Invented paths are omitted from the task and from the event payload.],
  [`plan`], [`ChangePlan`], [Triage and the citation labels, not the file bodies.], [Unsafe paths are removed. Prose that still names a removed path is replaced. `can_proceed` is then recomputed.],
  [`develop`], [`DevelopDiff`], [The approved plan, citation labels, the current approved files, and on a repair the previous review and test summary.], [Replacements become one unified diff. `git apply --check` runs before `git apply`. The proposal and the applied diff are stored as different bytes.],
  [`review`], [`ReviewResult`], [Plan, applied diff, citations, pytest output, and the baseline failure line. No file tool.], [Evidence that names a path absent from the diff, the test output, and the citations is omitted. Policy then chooses accept, repair, or failure.],
  [`supervisor`], [`SupervisorRecommendation`], [Phase, the allowed route names, title, and a description clipped to 500 characters.], [Used only when `SUPERVISOR_MODEL=true`. A route outside the allowed set is ignored. Invalid output leaves the rule's route in place.],
)

== What is not a model

These nodes are in the same graph and return to the supervisor, and they do not call `generate_structured`.

#table(
  columns: (1.2fr, 2.4fr),
  table.header([*Node*], [*Work it does*]),
  [`request_plan_approval`], [Calls LangGraph `interrupt()` and returns the person's decision. No database write and no file write happen before the interrupt.],
  [`prepare_workspace`], [Bare-clones the registered checkout under the workspace volume and adds a worktree. No fetch and no push.],
  [`test`], [Copies the worktree to the runner volume and waits for a result file. The worker does not start pytest.],
  [`request_publish_approval`], [A second `interrupt()`, before any `git push` or GitHub request.],
  [`publish`], [Pushes `patchpilot/<task-id>` and opens or reuses one draft pull request. There is no merge.],
  [`finalize`], [Checks the objects again and produces the business outcome the API stores.],
)

== How a call is wrapped

Every stage call goes through `generate_structured(prompt, schema, options)`. The graph does not talk to Ollama itself. `StageModels.for_stage` returns a small provider that looks up that stage's choice and then runs the call inside `CallMeter`.

#figure(
  image("diagrams/call.svg", width: 100%),
  caption: [One structured call. Capability, circuit, and budget can stop the call before any HTTP request. The local lock is taken only for Ollama. Pydantic validates the body. The usage row stores provider, model, latency, reported tokens, and outcome, not the prompt.],
)

#note[
  Ollama's JSON-schema grammar treats `maxLength` as a hard cut inside the generated tokens. On the develop schema that cut a function in the middle of a dictionary, and the file then failed to parse as Python. `old` and `new` are therefore plain strings in the schema. The whole object is still rejected above the parser cap of 8192 characters. A separate budget, `DEVELOP_SNIPPET_MAX_CHARS` (544), is what the fake provider and the tests enforce so a fixture stays inside that cap after JSON escaping is reserved. The grammar is not asked to stop mid-string.

  A second Ollama limit sits beside that one. The local coder model is loaded with `num_ctx` 4096. If generation continues past the leftover tokens, the window shifts and the start of the JSON falls out. `num_predict` is set from the prompt length, counting two characters as one token, with a 32-token margin. A prompt that already fills the window is given a single token rather than a sliding context. Thinking traces are turned off with `think: false`, because a trace is discarded and can be larger than the body cap the parser will accept (`MAX_OLLAMA_HTTP_BODY_BYTES` is 512 KiB, which exists only so a trace cannot exhaust memory before it is thrown away).
]

== What each model is allowed to change

The composition is a pipeline of typed objects with narrowing permissions.

#figure(
  image("diagrams/pipeline.svg", width: 100%),
  caption: [What each stage hands to the next. The upper row stops at a person. The lower row starts only after that approval, and only inside the private worktree. Policy, not the review model, chooses accept, repair, or failure.],
)

#table(
  columns: (1fr, 1.4fr, 1.5fr),
  table.header([*Object*], [*May influence*], [*Must not*]),
  [`TriageResult`], [Whether a repository task continues, and the text the later prompts quote.], [Name a path, write a file, or set `can_proceed`.],
  [`InvestigationReport`], [Which lines count as evidence, and therefore which existing `.py` files the planner is willing to treat as supported.], [Cite a line that was not read. A search hit is not a read.],
  [`ChangePlan`], [The path allowlist for the later patch, the problem statement, and whether approval is even offered.], [Write the checkout. `can_proceed` is recomputed after the path filter, and approval is a separate human step.],
  [`DevelopDiff`], [Up to three exact line-oriented replacements on approved paths.], [Return a unified diff or a full file. Choose a shell command. Touch a path the plan did not keep.],
  [`ReviewResult`], [Findings and a recommendation of accept, repair, or escalate.], [Accept a suite by itself. Policy can repair a passing recommendation, and it can refuse a repair the reviewer asked for.],
  [`SupervisorRecommendation`], [At most a choice between two routes the rule already listed.], [Approve a plan, write a file, raise a budget, or skip a node.],
)

#note[
  Most supervisor decisions have an allowed set of one route. The model, even with `SUPERVISOR_MODEL=true`, cannot move those. Two decisions have a real choice. When a valid plan named no permissible path, and a line was cited, and this is still the first plan, the rule's default is `replan` and the model may instead select `finalize`. When policy has asked for a repair and the repair count is still inside the cap, the rule's default is `repair` and the model may instead select `escalate`. A recommendation of `proceed_to_approval` on a task with no repository stays `finalize`, and the stored reason names the rejected route. The model's own reason text is not stored.
]

= The workflow

The graph is a `StateGraph`. `START` goes to `triage`. Every specialist node has an edge back to `supervisor`. The supervisor's conditional edges name the next node. `finalize` goes to `END`. The LangGraph `thread_id` is the task UUID, with no prefix. Each finished node is stored before the next node starts (`durability="sync"`). The graph stops after 24 supervisor decisions, after the supervisor deadline (default 2700 seconds from the first decision, hard maximum 3600), or at LangGraph's recursion limit of 40.

== The path a repository task takes

#figure(
  image("diagrams/flow-front.svg", width: 92%),
  caption: [From the bug report to plan approval. A "no" branch leaves the repository untouched. Investigate runs a second time only when no Python line was cited. Plan runs a second time only when every proposed path was forbidden.],
)

#figure(
  image("diagrams/flow-back.svg", width: 92%),
  caption: [After approval. Pytest runs on the unmodified worktree before the developer writes anything. A repair returns to develop at most twice. Publication is a second interrupt, and rejecting it finalizes the local task without calling GitHub.],
)

`investigate_more` and `replan` are further trips through `investigate` and `plan`. They are capped at one extra investigation and one extra plan (`MAX_INVESTIGATIONS` and `MAX_PLANS` are both 2, counting the first pass). `repair` is a further trip through `develop`, then the patched test, then review. The repair cap is 2 and cannot be raised by a setting.

== Routing rules

The supervisor reads the phase that just finished. It does not read the checkout, apply a patch, or start tests. A failed stage with no repair cycle finalizes. A failure during review, or any failure once a repair cycle exists, escalates, which also ends at `finalize` but records that a person needs to intervene. An unrecognized phase escalates. Counters that are not integers escalate. The step cap and the deadline escalate.

#table(
  columns: (1.15fr, 1.55fr, 1.3fr),
  table.header([*After*], [*Rule*], [*Next node*]),
  [Triage, no `repo_id`], [Investigation is skipped.], [`finalize`],
  [Triage, with `repo_id`], [A registered repository is attached.], [`investigate`],
  [Investigation, no `.py` citation, count \< 2], [Look again. Documentation and HTML do not unlock planning.], [`investigate`],
  [Investigation, otherwise], [The investigation is finished.], [`plan`],
  [Plan that names a change], [`can_proceed` and at least one modification.], [`request_plan_approval`],
  [Plan with only forbidden paths, evidence present, count \< 2], [Default `replan`. Model may select `finalize`.], [`plan` or `finalize`],
  [Plan that declines], [No approval is opened.], [`finalize`],
  [Approval resume, decision approve], [Status is `running`.], [`prepare_workspace`],
  [Approval resume, decision reject], [Terminal `rejected`.], [`finalize`],
  [Workspace, no baseline report yet], [The unmodified worktree has not been tested.], [`test`],
  [Workspace or baseline, report stored], [The private worktree is ready.], [`develop`],
  [Develop, patch stored], [A patch was applied.], [`test`],
  [Test, report stored], [A patched report is waiting.], [`review`],
  [Review, policy repair, count still inside the cap], [Default `repair`. Model may select `escalate`.], [`develop` or `finalize`],
  [Review, policy accept, publication configured], [Nothing has been pushed yet.], [`request_publish_approval`],
  [Review, policy accept, publication off], [The local task is done.], [`finalize`],
  [Review, policy fail], [A person needs to intervene.], [`finalize` as `failed`],
  [Publish approval, approve], [Push the task branch only.], [`publish`],
  [Publish approval, reject], [Finish locally.], [`finalize`],
  [Publish, draft number stored], [A draft pull request was opened.], [`finalize`],
)

#note[
  The baseline test is easy to miss because the same `test` node serves two phases. If `patch` is absent, the node writes `baseline_tests` and sets the phase to `baseline`. If a patch is present, it writes `tests` and sets the phase to `test`. The job id is different: the baseline id is a UUID v5 of the task id plus `:baseline`, the first patched pass uses the task id itself, and each repair uses a UUID v5 of the task id plus the repair count. Resuming the same pass finds the result file and does not ask the runner to execute pytest again. A repair publishes a new job id, so the suite runs again.

  The baseline exists so a repository that already fails some tests can still accept a patch. Policy compares pytest node ids. A patched run that only repeats failures already present in the baseline can be accepted. A new node id, a timeout, truncated output with no parseable identity, or an empty failure list is treated as a new failure. The comparison fails closed.
]

== Interrupts and resume

`request_plan_approval` and `request_publish_approval` call `interrupt()` before any side effect. LangGraph runs a resumed node from the top. A database insert placed above the interrupt would run again on resume. The worker observes the interrupt, and only then writes the approval row, sets `waiting_approval`, records `approval_requested`, and ends the lease so the task is not claimed again.

`POST /api/v1/tasks/{id}/decisions` accepts `approval_id`, `decision` (`approve` or `reject`), and an optional reason of at most 300 characters. The same decision again returns the stored row and does not queue another run. A different decision is HTTP 409. A new decision sets the task back to `queued`. The next claim resumes the same `thread_id` with `Command(resume=...)`. If resume still reports an interrupt, the task is stored as `failed` instead of being parked again.

A finished thread, reloaded by a new process, returns its stored outcome and does not call the model again. Answering an interrupt does not call the model either, until a later node such as `develop` reaches its own call.

= Tasks, claims, and leases

`POST /api/v1/tasks` validates a title (1–200 characters) and a description (1–20 000). Surrounding whitespace is removed. The row is inserted as `queued`. The caller cannot set status, stage, or results. `repo_id` is optional. An unknown id, a disabled id, a filesystem path, or a Git URL is HTTP 422 and stores nothing.

Statuses are `queued`, `running`, `waiting_approval`, `completed`, `rejected`, `failed`, and `cancelled`. `rejected` was added to the PostgreSQL enum early so a later approval flow would not have to alter the type.

== The claim

The worker polls. `WORKER_IDLE_SLEEP_SECONDS` defaults to 2. The claim takes the oldest queued row with `FOR UPDATE SKIP LOCKED`, opens a `task_runs` row, and commits. That commit releases the row lock before any model call. `lease_until` is the database clock plus `WORKER_LEASE_SECONDS` (default 30). A second worker does not take a task whose lease is still in the future.

#note[
  `generate_structured` is synchronous. While it runs, the worker's asyncio loop does not wake timers. A background thread, `LeaseKeeper`, opens its own connection and extends the lease. The interval is `min(max(lease / 3, 0.05), lease)`, so it is always shorter than the lease. The `UPDATE` matches only when the run id, the worker id, and an open run still line up, and only when `lease_until` is still in the future. Zero matched rows means the lease is already gone. The flag is set, the in-flight node raises `LeaseLost`, and that worker discards its result.

  There is a real overlap window. If a worker is still inside a model call after its lease has expired, the next poll may start a recovery while the first call is still running. Healthy workers renew in time and do not overlap. A lost lease does not include the database URL in the error.
]

== Recovery

When a lease expires, the next poll locks the task, closes the run with `lease_expired`, writes a `recovery` event, and starts the next attempt if the attempt count is below `WORKER_MAX_ATTEMPTS` (default 3). The new attempt resumes the LangGraph thread when a checkpoint exists, and otherwise replays from triage. Event writes use an idempotency key. `ON CONFLICT DO NOTHING` keeps the first payload. A replay does not insert a second copy and does not replace the first one.

Rows written in one transaction would otherwise share a timestamp. The insert uses `clock_timestamp()` plus `ordinal` microseconds, and the API orders by timestamp then id. The ordinal cap is 40 so one transaction can hold the route rows and the stage rows together.

When the expired attempt is already the last one, the task becomes `failed` with `recovery_exhausted` and stays there. Recovery retries expired leases. It does not retry a result the graph has already stored as failed.

The worker handles `SIGTERM`. That matters because it is PID 1 in the container.

= Registered repositories and read-only tools

Checkouts live under `REPOSITORY_ROOT`. The stored path is relative to that root. The database does not keep the host absolute path, a Git remote, or a token. `registration` records the checked-out commit. An optional `--github owner/name` records the repository a later publication may use. That pair is not a URL.

Registration is `python -m patchpilot.source register`. There is no HTTP route. The API has no authentication, and a request that reaches the container is not treated as an administrator. The API container does not receive the checkout mount. The worker does, read-only.

Compose uses `:ro,Z`. The `Z` option applies a private SELinux label when the worker container is created. A checkout added later can stay `user_home_t`, and registration then fails with `PermissionError` on `.git/HEAD`. `scripts/bootstrap.sh` copies the mount label onto those files before it registers them.

`scripts/init_demo_repo.py` creates `examples/demo_service/.git` and `examples/minitask/.git`. The parent repository ignores both. Adding either directory while the nested `.git` exists records a gitlink. `scripts/add_all.sh` removes those directories, runs `git add --all`, and recreates the checkouts. It does not commit.

== The three tools

`list_tree`, `read_text_file`, and `search_text` each take a relative path under that one checkout. They are used by the investigator. The limits below are the production defaults. A caller may pass a smaller `ToolLimits`. A value above the hard ceiling is rejected, so a setting cannot turn the limit off.

#table(
  columns: (1.4fr, 0.8fr, 0.9fr),
  table.header([*Limit*], [*Default*], [*Hard maximum*]),
  [Tree entries], [200], [2000],
  [Tree depth], [6], [12],
  [File bytes], [32768], [262144],
  [File lines returned], [400], [2000],
  [Files searched], [100], [1000],
  [Search matches], [40], [200],
  [Query length], [200], [200],
  [Line length stored], [200], [400],
)

A path is a slash-separated relative path. Absolute paths, `~`, a drive-style colon, backslashes, NUL, `..`, empty segments, and dotfiles are `invalid_path`. Each segment must match a conservative character class. Denied names are omitted from listings and cannot be read: `.git`, VCS metadata, `.ssh`, `.aws`, `.env*`, key files, `credentials.json`, caches, virtualenvs, and suffixes `.pem`, `.key`, `.p12`, `.pfx`, `.kdbx`. Readable text names are a suffix allowlist (`.py`, `.toml`, `.md`, `.html`, and similar) plus a few bare names such as `readme` and `dockerfile`. Search is a literal substring and does not follow symlinks.

#note[
  Symlink checks are not enough to keep a read inside the checkout. A hard link does not change path resolution. `realpath` of a checkout path stays inside the checkout even when another directory entry for the same inode lives outside it. `is_hard_linked_file` refuses a regular file whose `st_nlink` is greater than 1, on read, on patch apply, and on the runner copy. A file that is hard-linked only to another name inside the checkout is refused as well. That is fail-closed.

  The check and the subsequent `open` are still two steps. A file can be replaced between them. The operator who points `REPOSITORY_ROOT` at a tree is trusted to name the tree they mean. Registration is a local command for that reason.
]

= The investigator

The investigator runs only after triage has succeeded and the task names a repository. It may call the three tools until a budget ends. The default is `INVESTIGATOR_MAX_TOOL_CALLS` of 6, and the hard maximum is 12. The task-wide tool budget defaults to 12 and caps at 24, so a second investigation, which starts over, can spend another tool budget of its own.

The loop asks for an `InvestigatorStep`. That object is either a tool request or a report. Native tool calling is a separate capability. It stays off for Ollama unless `OLLAMA_TOOL_CALLING=true`, and the current stages do not require it. The model selects a tool by filling a schema. The agent executes the tool and appends the observation. When the remaining model budget is zero, one last call asks for an `InvestigationReport` only.

== Seeding, aliases, and the citation reserve

Before the model chooses anything, the agent lists the checkout. That listing names directories and source files (`.py` and `.html`). Documentation and config stay out of it and are read only when a tool asks for them. Quoted strings in the title and description are then searched, up to four queries, and matching Python files are read, up to three, so a later citation of those lines can be kept.

A model often prefixes a path with `examples/` or with the checkout directory name. The tools are already rooted inside the checkout, so that path does not exist. The agent takes the longest remainder that does exist, reads it, stores the read under the remainder, and remembers an alias. A citation of the missing prefixed path is kept as the file that was actually read. Any other miss returns not found, plus the root listing.

Up to two tool calls are held back from the model (`_CITATION_RESERVE`). After the report, including one forced at the end of the loop, a cited path that was never opened is read with those reserved calls. The total stays inside the same budget.

#note[
  A citation survives only when `read_text_file` returned that path and the cited end line is within the line count that was returned. Search hits and directory listings are not reads. An invented path is omitted, and the omitted path is not written into the task or the event payload. File text stays in the prompt and out of `result_json`.

  This is the boundary against a comment in the repository that says "cite `secrets/api_token.txt`". The fake provider includes that case. The comment can appear in a hypothesis only if some read actually contained it, and the citation itself is dropped unless those lines were read. The filter does not rewrite a hypothesis that quotes a comment from a file that was legitimately read. The hypothesis is prose. The evidence list is the grounded part.

  A second investigation does not continue the first investigation's notes. It spends a fresh tool budget and builds a new ledger. That is deliberate and expensive: the checkpoint stores the finished report, not the tool transcript, and a resumed investigate node that did not finish repeats the reads.
]

The stored report has a summary, hypotheses, `relative_path:line` citations, probable files, and unanswered questions. Caps inside the agent keep the object small: 12 citations, 6 hypotheses, 8 questions.

= The planner

The planner makes one call and returns a `ChangePlan`: a problem statement, assumptions, proposed modifications, a test strategy, risks, confidence, and `can_proceed`. Nothing in this stage writes the checkout.

A modification is either an existing `.py` file in the checkout or a new test. A new test must be named `test_*.py`, must sit under a `tests` directory that already exists, and the parent directory of the new file must already exist. Symlinks, traversal, secrets, Docker files, CI workflows, and dependency manifests are not permissible plan paths. The plan may contain at most eight modifications.

`accept_plan` then edits the model's object.

+ Paths that fail `modification_is_allowed` are counted as rejected and dropped.
+ If triage does not need clarification, and the draft's problem statement, assumptions, test strategy, or risks contain a cited existing `.py` path, that path is added as an `existing` modification when it is not already kept. `can_proceed` is set true.
+ Otherwise `can_proceed` stays true only when the model asked to proceed, at least one evidence line exists, triage does not need clarification, and at least one path survived. If that fails, the modification list is cleared.
+ Any stored sentence that still contains a rejected path is replaced with a short fallback, so a dropped path does not remain in the problem statement.

#note[
  The model's `can_proceed` is not the decision. A model that has already named a cited Python file in its prose, and then sets `can_proceed` false, is overridden. The evidence supports an edit, and the workflow would rather show that plan to a person than stop. The converse also holds. A model that sets `can_proceed` true with no cited line, or with only forbidden paths, is stored as a decline.

  The override matches paths by substring. A sentence that mentions a cited path in order to refuse it still counts as naming it. That is a real sharpness in the rule: the check is "does this string occur", not a parse of the sentence.

  `can_proceed` still does not authorize a write. It only decides whether the supervisor offers the plan to a person. Unsafe paths never reach that offer. A plan whose every path was forbidden, while some evidence exists, is replanned once. The model may choose to finalize instead. A deliberate decline with no rejected path is not replanned. Invalid plan JSON fails the task at `plan`. Replan is for a schema-valid plan that the filter emptied.
]

A finished plan, including one that is waiting for approval, is a versioned artifact at `{task-id}/plan/v{n}.json`. The same bytes keep the same version. `GET /api/v1/tasks/{id}/plan` returns the newest revision, and `?version=` returns an older one. The response has no filesystem path. If the database transaction rolls back after the file is written, the file can remain without a row. A checksum mismatch is returned as a missing plan.

= Human approval of the plan

A plan that names a code change pauses. A declined plan never opens an approval and completes at finalize. Rejection is terminal `rejected`, with the reason as the summary, or `The plan was rejected.` when no reason was sent. The registered checkout is unchanged either way. Approval does not itself apply a patch. It allows `prepare_workspace`.

One task has at most one pending approval of a given kind. A second pause reuses that row. A decision is accepted only while the task is `waiting_approval`. The dashboard buttons post JSON to the decisions route. The HTML page has no second form endpoint, so recording a decision needs JavaScript. Reading the page does not.

= The private workspace

`prepare_workspace` runs only after an approval whose decision is `approve`. It does not read the plan's paths as destinations. Paths are derived from the task id: `WORKSPACE_ROOT/<task-id>/clone` and `WORKSPACE_ROOT/<task-id>/worktree`, on branch `patchpilot/<task-id>`, at the source HEAD. A checkpoint value that names another path is rejected. PostgreSQL check constraints tie the stored relative paths and the branch to the task id.

The clone is bare, made with `--no-hardlinks`. `origin` is removed. `git worktree add` runs against that clone. The registered `.git` is not a worktree parent, and the function fails if `.git/worktrees` appears on the source. Protocols other than `file` are refused. Hooks are pointed at `/dev/null`. There is no fetch and no push. `safe.directory` is set for the source, the clone, and the worktree so a checkout owned by another user can be read. The command list is a fixed set: `rev-parse`, `clone`, `remote-remove`, `remote-list`, `worktree-add`, `worktree-remove`. It does not take a path from the model.

#note[
  The baseline is the source HEAD. Uncommitted files in the registered checkout are not copied. A second prepare reuses the worktree when the branch and commit still match, including edits already in that worktree. That reuse is what makes a crash after the clone and before the row commit recoverable, and it is also why a dirty worktree is not silently reset.

  If the workspace row cannot be stored, the worker deletes the task directory and marks the task failed. Completing a task leaves the worktree in place. `WorkspaceRepository.retire` deletes the directory and marks the row `removed`. A failed patch or a failed review deletes that task's directory. Retirement of a success is explicit.

  A checkpoint that already moved past approval on an older graph, and is waiting on an edge that no longer exists, fails closed on resume. A task parked on the interrupt resumes into `prepare_workspace`.
]

= The developer and the patch

The developer runs only in the private worktree, and only when the plan was approved and `can_proceed` is true. The registered checkout is not modified.

The model returns a `DevelopDiff`: a summary and one to three `SnippetEdit` values. Each edit is a path, an `old` span, and a `new` span. The prompt tells the model to copy `old` verbatim from the file, to use complete lines, and to leave `old` empty only for a new test file. It also says not to return a diff or the full file. The response cap is why. A full file or a unified diff from the model would have to fit in the same bounded JSON object, and the earlier `maxLength` experiment showed that a grammar cut produces Python that does not parse.

The agent renders those replacements as one unified diff with `difflib`, wraps them in a `PatchProposal`, and calls `apply_patch`. The proposal stored as an artifact is that rendered document. The applied artifact is the diff Git recorded after the edit. They have different sha256 values. `GET /api/v1/tasks/{id}/patch` returns both. `result_json.patch` stores the summary, the paths, and the two checksums. It does not store the diff text.

== What `apply_patch` refuses

A path is writable only when the approved plan named it and it is still an existing `.py` file or a new `tests/test_*.py` file. The apply policy repeats the denylist: symlinks, hard links, secrets, `.git`, CI segments (`.github`, `.gitlab`, `.circleci`, `workflows`), Docker and Compose names, dependency manifests (`pyproject.toml`, lockfiles, `package.json`, `go.mod`, and the rest of that list), traversal, binary bytes, and a mode other than a normal file. `git apply --check` runs before `git apply`. The command list is fixed. There is no shell. A diff larger than 16384 bytes is refused. The artifact table still allows a larger row. The refusal happens before Git applies anything.

On a repair the prompt includes the previous review and a short test summary, and it says an unchanged file is rejected. The files shown to the model are the previous attempt, so the next edit has to move.

If apply fails with `syntax` or `mismatch`, the agent makes one more model call. The prompt gains a short refusal: the previous edit was not valid Python, or the previous `old` text was not found once. A second failure of either kind, or any other `PatchError`, fails the stage. A failed patch deletes that task's workspace directory. The source checkout is unchanged.

#note[
  `mismatch` is the sharp edge of exact replacements. `old` must occur once. A model that paraphrases a line, or that includes a trailing space the file does not have, is rejected and given one correction. That is more reliable than asking a small local model for a unified diff, which has to get hunk headers and context counts right, and more reliable than asking it for a whole file, which blows the context window and the grammar limit.

  The same rule hurts recovery. If a patch is applied and the worker then loses its lease before the node is checkpointed, the retry finds the worktree already edited. The original `old` text is gone, so the new proposal mismatches, and the failed attempt deletes the workspace. The source tree is still untouched. The dirty worktree is not kept for inspection.

  `git apply --check` failure is reported as a malformed patch and leaves the worktree unchanged because the check runs first. A symlink anywhere in the worktree makes the snapshot refuse the patch.
]

There is still a `PatchProposal` form with full-file edits and an expected sha256, used by older fixtures. The develop model is not asked for that form. The agent accepts a `PatchProposal` if one is injected, and otherwise converts a `DevelopDiff`.

= The restricted test runner

The worker copies the private worktree into `RUNNER_ROOT` and writes a request whose only fields are `schema_version` and `job_id`. A separate Compose service, `python -m patchpilot.runner`, reads that file and runs one fixed pytest command in that job directory. A request that names a command, a path, or an argument is stored as `invalid_request` and is not executed. The worker waits for the result file. It does not start pytest.

Compose runs the runner as uid 1000 with `network_mode: none`, a read-only root, a writable `/runner` volume and `/tmp`, every capability dropped, `no-new-privileges`, and CPU, memory, and PID limits. `RUNNER_TIMEOUT_SECONDS` bounds the pytest process. `RUNNER_WAIT_SECONDS` must be greater than that timeout. Settings reject a configuration where the worker can give up before the runner is allowed to finish. If the worker stops waiting anyway, the task fails at `test` and a late result is unused.

The runner refuses to start when its environment contains a credential or a model setting. The check looks at a fixed set (`DATABASE_URL`, `POSTGRES_PASSWORD`, `LLM_PROVIDER`, `OPENAI_API_KEY`, and others) and at name fragments such as `password`, `secret`, `token`, and `api_key`. Pytest's own environment is an allowlist: `PATH`, locale, `HOME`, `TMPDIR`, `VIRTUAL_ENV`, `PYTHONPATH`, `PYTHONHOME`. A proxy or an extra sentinel set on the runner is not copied.

Output kept in the report is bounded (2000 characters in the tests' truncation checks; a process that writes more than 256 KiB is cut off). Paths from the runner volume are not included in the report. A passing, failing, or timed-out suite is a `test-report` artifact. `GET /api/v1/tasks/{id}/tests` returns the exit code and the bounded output. `result_json` keeps the status and the checksum.

#note[
  The runner and the worker share one image, so pytest is installed where the API can see it. The API does not invoke it. Host tests run the runner as a process. They check the Compose file for `network_mode: none`. They do not themselves create a network namespace.

  A symlink in the copied tree refuses the job. The copy is deleted after the worker reads the result. The result file stays, which is what makes resume idempotent. A crash before that read leaves the copy in the volume.

  Generated Python that policy later accepts still runs under pytest inside this boundary. The network, capability, user, and resource limits are the containment. They are not a claim that the generated code is safe to run anywhere else.
]

= The reviewer and the repair policy

The reviewer sees the approved plan, the applied diff, the investigation citations, the sandbox report, and a line describing baseline failures. It returns findings, a severity, evidence, and one action: `accept`, `repair`, or `escalate`. It has no file tool. Evidence that names a path absent from the diff, the test output, and the citations is omitted.

`decide_review` ignores the recommendation when it disagrees with the measurements.

+ A diff that is not a `diff --git` document, that contains a NUL, that exceeds 16384 bytes, or that names a path outside the approved plan, fails immediately. The summary says a person needs to intervene. The apply tool already refuses that diff. This is a second check, and it runs before the reviewer is called.
+ `escalate` fails without a repair.
+ If the suite did not pass, but `has_new_failure` is false, and every finding's evidence is a substring of the baseline output and is not a substring of the diff, policy accepts. The reason is that the remaining failure is in the baseline.
+ A critical finding paired with anything other than `repair` fails.
+ `accept`, a passing suite, and no critical finding accepts.
+ Otherwise, while `repair_count` is below the cap, policy repairs. The reason starts with "Tests failed.", "A critical finding needs a revision.", or "The reviewer requested a revision."
+ A third request fails. A failing suite that has used both repairs fails even if the reviewer keeps saying `accept`. A non-critical repair request that has used both repairs fails even if the suite is green.

Each repair writes a `repair` event. The developer then runs again, then a new test job, then review. The proposal, the applied diff, and the review are separate artifacts with different checksums. `GET /api/v1/tasks/{id}/review` returns the verdict and the policy decision, not the diff.

#note[
  The baseline-remainder rule is the careful one. A reviewer can quote the diff and call a pre-existing failure a new defect, or quote the new test and claim the old failure is unchanged. Policy accepts the "already failing" explanation only when three things hold at once: the patched run introduces no new pytest node id, the suite is not a timeout, and every finding's evidence text occurs in the baseline output and does not occur in the diff. Quoting the patch disqualifies the finding from that exception. An empty evidence string does not qualify. If the patched output is truncated or has no parseable `FAILED` or `ERROR` line, `has_new_failure` returns true, and the exception does not apply.

  Failure identities are extracted with regular expressions for `FAILED <node>`, `ERROR <node>`, and `ERROR collecting <path>`. That is good enough for the pytest output this runner produces, and it is not a general test-runner parser. A suite that fails without printing those shapes is treated as a new failure.

  The reviewer does not choose the repair count. Policy does. `require_repair_budget` rejects a value above 2. There is no environment setting that raises it.
]

A failed review stores the proposal, the diff, the test report, and the review, then deletes the task workspace. The registered checkout is unchanged.

= Publication

Publication is off unless all of the following hold: `GITHUB_ENABLED=true`, the worker has `GITHUB_TOKEN`, the registered repository was stored with `--github owner/name`, and that `owner/name` is listed in `GITHUB_ALLOWED_REPOS`. The API and the runner do not receive the token. `github_api_base` must be `https://api.github.com`. Any other host is a settings error. The HTTP client has no merge method.

After policy accepts a patch, and publication is configured, the graph pauses at `request_publish_approval`. Nothing is pushed while that approval is pending. Approving it pushes only `patchpilot/<task-id>`, never `main` or `master`, and never a refspec that starts with those names. The branch must be the current HEAD of the worktree, and the HEAD must be unchanged across the push. The token is passed through an askpass script and an environment variable. It is not written into the remote URL or the repository config. After the push, the config is scanned for the token, and a hit is rejected.

The pull request is a draft. The body contains the issue, the plan's reasoning, a compare link, the test result, and a machine-generated disclosure. An existing open draft for that head is reused, so a retry does not open a second pull request. A response that is not a draft, or whose URL does not match the allowed repository, is rejected. Rejecting publication finishes the local task, writes no publication row, and does not call GitHub.

`GET /api/v1/tasks/{id}/publication` returns the draft. It does not include a token.

#note[
  The push refuses `main` and `master` twice: once on the branch name derived from the task id, and again on the refspec string. The task-id branch cannot be `main` unless the layout function is changed, and the second check is there so a future layout change cannot make the refspec point at the default branch without failing this function. The client still has no merge. A published task is a draft on a side branch.
]

= Model routing, budgets, and metering

`LLM_PROVIDER` defaults to `ollama` in application settings. The Compose stack can be pointed at `fake` by setting the provider and clearing `LLM_STAGE_MODELS`. Cloud stays off until `LLM_CLOUD_ENABLED=true` and a base URL, an API key, and a capability list are set. A stage that needs `structured_output` fails closed when the cloud capability list omits it. The worker is the only service that receives the key.

Invalid JSON, timeouts, and HTTP 408, 429, 502, 503, and 504 wait `LLM_RETRY_BACKOFF_SECONDS` between attempts (default 0.5, maximum 8). HTTP 500 and connection failures are not retried. Two invalid outputs for the same stage, provider, and model open a circuit. The third call does not reach the provider and is stored as `circuit_open`.

== Ceilings

A task stops when it exceeds one of these. Defaults are also the values a normal `.env` is expected to carry. Hard maxima are rejected at settings load, except where the code rejects them again at use so a caller cannot pass a larger number around settings.

#table(
  columns: (1.5fr, 0.8fr, 0.9fr, 1.6fr),
  table.header([*Ceiling*], [*Default*], [*Hard maximum*], [*Notes*]),
  [Model calls], [24], [48], [Seeded from `model_calls` on resume, so a new process does not start at zero.],
  [Tool calls], [12], [24], [Charged as the investigator runs. A resumed investigation can spend another tool budget. The per-investigation cap is separate and smaller.],
  [Reported tokens], [100000], [500000], [Counted only when the provider reports them. A call with no token count does not spend this budget.],
  [Task runtime], [900 s], [3600 s], [Wall clock on the budget object.],
  [Supervisor deadline], [2700 s], [3600 s], [From the first supervisor decision. Not the same clock as the task runtime.],
  [Supervisor steps], [24], [24], [A constant. An environment setting cannot raise it.],
  [Graph recursion], [40], [40], [LangGraph's limit for this compile.],
  [Parallelism], [1], [1], [`TASK_MAX_PARALLELISM=2` is a validation error.],
  [Repairs], [2], [2], [Policy and the supervisor both stop here.],
  [Investigations], [2], [2], [The second pass is only when no `.py` line was cited.],
  [Plans], [2], [2], [The second pass is only when every proposed path was forbidden.],
  [LLM attempt timeout], [1800 s], [1800 s], [Per attempt. A cold local model can use most of that. Retries add further attempts.],
  [Diff bytes], [16384], [16384], [Enforced before `git apply`.],
)

#note[
  Three clocks cover one task, and they are not nested cleanly. The task runtime budget defaults to 900 seconds. One Ollama attempt may wait 1800 seconds. The supervisor deadline defaults to 2700 seconds. A cold model can therefore exhaust the task budget inside a single call, while the supervisor deadline has not started to matter, and a retry of that call is a further 1800 seconds the budget will refuse. The attempt timeout is a per-request socket timeout. The task budget is the workflow's wallet. The supervisor deadline is a backstop for a graph that keeps routing. They were set for different failures, and an operator who expects one number to bound the whole task will be surprised.

  Token totals are partial by construction. Fake fixtures report 20 prompt tokens and 10 output tokens on success, and calls that raise report none. A live model that omits usage is stored with null counts. Cost is `unknown` unless `LLM_PRICING_PATH` points at a local JSON file whose entry for that exact model has `verified: true`. The file is not fetched from the network. A price that is not in it is not invented. If any call in the task lacks a verified price or a token count, the usage payload does not add a partial total.

  `bool` is a subclass of `int` in Python. Budgets, ordinals, exit codes, and counters all reject a boolean explicitly. A JSON `true` that landed in a count would otherwise pass `isinstance(value, int)` and become 1. That check is repeated because these values cross a schema boundary more than once: model output, checkpoint state, and HTTP bodies.
]

`GET /api/v1/tasks/{id}/usage` reads the stored rows and does not call a model. Outcomes include `success`, `transient_error`, `permanent_error`, `invalid_output`, `budget_exhausted`, `circuit_open`, and `capability_rejected`. Tests that inject a provider directly do not meter. The worker builds the router, and therefore the records, only when it creates the provider itself.

Local Ollama calls take `LocalGate`, an in-process slot, and `pg_try_advisory_lock` on a fixed key. The connection stays open until release. Process exit releases the lock. Fake and cloud calls do not take it. The lock serializes use of the host GPU across workers. It is not a fairness queue. A lock error does not include the database URL.

= What is stored, and the audit trail

Alembic owns the business schema. Head includes tasks, task runs, events, repositories, artifacts, approvals, workspaces, model calls, and publications. `AsyncPostgresSaver.setup()` creates the four checkpoint tables. The command `python -m patchpilot.checkpoints` is idempotent and does the same setup the worker runs on start. The API does not open the checkpointer. Alembic's version table does not mention the checkpoint tables, and that split is intentional: LangGraph migrates its own tables, and the application does not want a second writer for them.

Events are append-only. `UPDATE` and `DELETE` on `events` are rejected. Event names include `stage_started`, `stage_completed`, `model_error`, `task_completed`, `task_rejected`, `approval_requested`, `approval_decided`, `retry`, `recovery`, `repair`, `route`, and `publication_opened`. A `route` event records the route name, the reason, and the phase that had just finished. Payloads are small. Fields whose names look like passwords, tokens, or database URLs are stored as `[redacted]`.

`GET /api/v1/tasks/{id}/events` returns the trail oldest first, capped, with `truncated` when the page is shorter than the table. A missing task is HTTP 404. A task with no events returns an empty list.

#note[
  Idempotency is load-bearing for recovery. The key is unique. The conflict clause does nothing, so a replayed stage cannot change the reason that was recorded the first time. That also means a bug in the first payload cannot be repaired by running the stage again. The first write wins, including a wrong one.

  Route events are written when the run is stored. On a resumed run, decisions already stored are not inserted again. The payload index is the decision order.

  Checkpoint blobs are the place model text can rest. The API does not serve those tables. Operators who copy the database are copying that text too.
]

= The dashboard

`GET /` is server-rendered HTML. The task list and the create form are on that page. Creating a task posts JSON to `POST /api/v1/tasks` and opens the task page. The task page shows status, stage, the report, triage, evidence citations, the plan, approval, the applied diff, the pytest report, the review, publication, and model usage. Approve and reject post JSON to the decisions route.

The task page polls `GET /tasks/{id}/progress` every 2 seconds. The response contains events after the last id already on the page, and the script skips an id that is already present. A status or stage change reloads the page from the database. Plan, diff, tests, review, and usage appear on that reload. The progress poll itself is not a second copy of those artifacts.

Reading the page does not need JavaScript. Creating a task and recording a decision do. The layout is one column below 40rem. The repository select lists enabled registry rows by display name. It does not browse the filesystem.

#note[
  Redaction runs before render. Connection strings, bearer tokens, `sk-` keys, secret-like JSON field names, and configured secrets of at least eight characters are replaced. A configured secret shorter than eight characters is not stripped from free text. Those other patterns are still removed. Artifact filesystem paths are not shown. Cost stays `unknown` unless a stored call was estimated from a verified price.
]

= The benchmark

`python -m patchpilot.benchmark --provider fake` runs 15 curated issues through the worker and writes `report.json`, `report.md`, and `report.html` under `benchmark-results/`. The issues cover a successful fix, one repair that then passes, triage with no repository, ambiguous and unclear reports, missing evidence, a named file that is not in the tree, tests that keep failing, a rejected approval, invalid plan JSON, invalid patch JSON, a hostile source comment, a bogus citation, an unsafe path request, and a lease expiry that a second claim recovers.

`--provider fake` does not call Ollama or a cloud API. Fixture-only faults are appended to the model prompt and are not part of the stored issue text. `--provider ollama` sends the same issue texts to the configured local model and skips the faults only the fixtures can force. The CLI does not offer a cloud provider. The harness creates the checkpoint tables itself. `DATABASE_URL` must point at a migrated application database.

The report records classification, whether stored citations were read, a plan proxy (`can_proceed`, dropped paths, and safe surviving paths), patch apply rate, test pass rate, policy violations, retries, latency, and reported tokens. A policy check fails if a registered checkout changes, or if a dropped path, the injection marker, or the fixture token is stored. The scores are regression measurements for this repository. They do not represent industry benchmark quality and they are not SWE-bench.

#note[
  Several faults exist only in `FakeProvider`: invalid JSON, a patch that leaves tests failing, and one forced repair. A live model is not asked to reproduce them, and the ollama mode skips those cases. Fake token counts are the fixture values, not a tokenizer measurement. Latency is wall-clock time and is not a pass/fail target. Planning quality is a proxy. It does not rate the plan's prose. The recovery case stalls the first model call and expires the lease. It does not kill a container.
]

= Threat model

The person running Compose is the administrator. The model is untrusted. Repository text is untrusted. A task description is untrusted.

The design is built to prevent the following.

- The API and the runner do not receive the model key or the GitHub token. The runner exits if its environment contains a secret, and pytest inherits an allowlist.
- A task cannot name an arbitrary path or Git URL. Read tools reject `..`, absolute paths, symlink escapes, `.git`, credential names, binaries, oversized files, and hard links.
- A patch is applied only after plan approval, only in the private worktree, and only on paths the approved plan still allows. `git apply --check` runs before `git apply`. There is no shell.
- A second approval is required before a task branch is pushed. The client has no merge method. The push refuses `main` and `master`. A repeated decision does not open a second pull request.
- A crashed worker loses its lease. Recovery resumes the checkpoint until `WORKER_MAX_ATTEMPTS`, then the task stays `failed`.
- Supervisor rules choose the route. A model recommendation outside the allowed set is ignored. Review policy, not the reviewer, chooses accept, repair, or failure.
- Citations require a read. A prompt-injection comment cannot add a path the tools did not open.

The design does not claim the following.

- The localhost API is an authenticated multi-user service. Anyone who can open `127.0.0.1:8000` can create tasks and record decisions. Do not publish port 8000.
- The shared runner volume isolates two jobs from each other. Concurrency is 1 so that a second job is not staged at the same time.
- A registered checkout is the tree the operator named. Registration is a trusted local action.
- Generated Python that policy accepts is safe outside the runner. Inside the runner it executes with no network and with resource limits. That is the boundary.
- Checkpoint blobs are free of model text. They are not. The API does not serve them.
- Ollama is an authenticated service. PatchPilot sends no credentials to it. Anyone who can open a TCP connection to the port can use the model. Keep port 11434 off the LAN. `OLLAMA_HOST=0.0.0.0:11434` listens on every interface. On Fedora, leave 11434 closed in firewalld, or bind Ollama to the Compose bridge address only. Packets that arrive on the bridge address do not reach a loopback socket, so a worker container cannot use an Ollama that listens only on `127.0.0.1`. `scripts/bootstrap.sh` sets `OLLAMA_HOST` for that reason on Linux and Podman, and it does not open the port in firewalld. Docker Desktop can already reach a loopback Ollama through `host.docker.internal`, so the script leaves that bind alone. Compose does not publish port 11434.

= Current limitations

This section is the state of version 0.1.0, not the historical notes in `docs/progress.md`. Several early limits were removed on purpose: hard links are now refused, a failing suite is no longer marked completed just because a report exists, and publication cannot target the default branch. What follows is still true.

== Product scope

- Patches are Python. An existing change must be a `.py` file. A new file must be a pytest module under an existing `tests` directory. Docker files, CI workflows, dependency manifests, and every other name are refused even when the bug is in them.
- The only executed check is one fixed pytest command. The model cannot select a test runner, a file, or an argument. A repository whose tests are not pytest does not get a meaningful signal, and policy will treat the resulting output as a failure or as unparseable.
- There is no merge and no deploy. Publication, when configured, opens a draft pull request on `patchpilot/<task-id>`.
- The investigator's tools are read-only and bounded. A checkout that needs more than a few hundred files of search, or files larger than 32 KB, is only partly visible. Files over the line cap return the first 400 lines with `truncated` set. A citation cannot point past the lines that were returned.
- The planner may name an existing `.py` file that was not itself cited, once any line was cited, provided the path is allowed. A new test path does not have to be cited.
- Uncommitted work in the registered checkout is invisible. The worktree is the committed HEAD.
- Successful worktrees are not deleted automatically. The volume grows until something calls retire. Failed patch and failed review do delete that task's directory.
- The dashboard cannot register a repository, edit a plan, or edit a diff. Registration is a shell command. Decisions are approve or reject.

== Models and evaluation

- Default local tags are `qwen3.5:4b` and `qwen2.5-coder:7b`. They are a fit for a small GPU, not a claim about repair quality. A cold load can spend most of the 1800 second attempt timeout.
- Native tool calling is off. The investigator parses a JSON tool request. Models that only support free-form tool-call channels are not driven by this loop.
- `FakeProvider` makes the automated demo and the benchmark deterministic. Those scores measure this repository's fixtures. They are not an industry benchmark.
- The benchmark's live-model mode skips the faults only fixtures can force. Invalid JSON, a patch that never fixes the tests, and a forced repair are not measured against Ollama.
- Cost is unknown without a hand-written price file that marks the exact model `verified`. Token totals ignore calls that report no usage.
- The task runtime budget (900 seconds), the per-attempt timeout (1800 seconds), and the supervisor deadline (2700 seconds) are three different clocks. One setting does not bound the others.
- A second investigation discards the first pass's tool transcript and spends another tool budget.
- The develop stage allows one correction after `syntax` or `mismatch`, then fails. It does not search for a nearby span.
- If the lease is lost after `git apply` and before the node checkpoints, the retry mismatches on `old` text that is no longer in the file, and the workspace is deleted.

== Isolation and operations

- Worker concurrency is 1. The shared runner volume and the single Ollama slot are the reasons. This is not an oversight in the poll loop.
- The runner does not get a fresh container mount per job. Two jobs are kept apart by not existing at the same time.
- A lease that expires during a blocking model call can overlap with the recovery attempt until the first worker notices and discards its result.
- Tool-call counts live on the running task. Model-call counts, reported tokens, and the invalid-output streak are seeded from `model_calls`. A resumed investigation is not seeded with the previous tool total in the same way.
- Checkpoint tables are created by `AsyncPostgresSaver.setup()`, not by Alembic. Operators who only run `alembic upgrade head` and never start the worker, and never run `python -m patchpilot.checkpoints`, do not have a graph store.
- The API has no authentication and is published on localhost. Anyone who can open that port can create tasks and approve plans.
- Dashboard redaction does not strip a configured secret shorter than eight characters from free text.
- Creating a task and recording a decision require JavaScript.
- Screenshot files named in the README are not in the repository until someone captures a live run.
- mypy checks `src/patchpilot`. It does not check `tests/`.
- GitHub publication has been tested with a mock transport and a local bare remote. A live call to `api.github.com` is an operator action and is not part of the default demo.
- Host Ollama is optional. Binding it beyond loopback is a separate choice, described in the threat model. The worker container cannot reach an Ollama that listens only on `127.0.0.1`.

== Sharp edges inside the rules

- Baseline acceptance depends on pytest's `FAILED` and `ERROR` lines. Other failure shapes count as new failures.
- The planner's "this prose names a cited file" check is a substring test.
- A hypothesis can repeat text from a file that was read, including a hostile comment. The citation list cannot point at a file that was not read.
- Search does not follow symlinks, including a symlink whose target stays inside the checkout. `read_text_file` follows an internal symlink and reports the target path.
- Event replay cannot correct a payload. The first idempotent write wins.
- A plan file can be left on disk if the database transaction rolls back after the write. The API treats a checksum mismatch as a missing plan.
- A checkpoint from an older graph shape fails closed when the next edge no longer exists, rather than guessing a node.

= Running and verifying

On Fedora or macOS, `./scripts/bootstrap.sh` installs what is missing and starts the stack. It is safe to run again. An existing `.env` is kept. The demo checkout is initialized once. A model that is already present is not pulled again. `--skip-models` skips the download. `--no-start` prepares the machine and does not run Compose. `--skip-system` does not install operating-system packages.

The API image runs `alembic upgrade head` before it serves traffic. The worker runs checkpoint setup on start. Readiness is `GET /health/ready`, which opens PostgreSQL and runs `SELECT 1`. Liveness is `GET /health/live` and does not touch the database.

Database credentials in `.env.example` are development examples. Copy `.env.example` to `.env` to override them. `.env` is gitignored. Point a host process at `localhost`. Compose uses the `db` hostname. The Compose database port is not published.

#table(
  columns: (1.3fr, 2.2fr),
  table.header([*Check*], [*Command*]),
  [Unit tests, lint, types], [`uv run pytest` \ `uv run ruff check .` \ `uv run ruff format --check .` \ `uv run mypy`],
  [Offline model call], [`uv run python scripts/check_llm.py --provider fake`],
  [One in-process workflow], [`uv run python scripts/check_workflow.py --once`, with `DATABASE_URL` and a migrated database. No other worker should be polling.],
  [Workflow against Compose], [`uv run python scripts/check_workflow.py --api-url http://127.0.0.1:8000`],
  [Offline benchmark], [`uv run python -m patchpilot.benchmark --provider fake --database-url "$DATABASE_URL"`],
  [CI], [GitHub Actions runs Ruff, mypy, and pytest with `LLM_PROVIDER=fake`, cloud and GitHub disabled, and `GITHUB_TOKEN` blank.],
)

Task tests start a disposable PostgreSQL 16 container with Podman or Docker, apply migrations, and leave the Compose volume alone. Health tests do not need a running database. The readiness failure test connects to a closed local port and checks that the password is not returned or logged.

A five-minute walkthrough of the unknown-tier issue is in `docs/DEMO.md`. The decisions behind the worker split, the graph, PostgreSQL checkpoints, deterministic routing, and the restricted runner are in `docs/adr/0001` through `docs/adr/0005`. `docs/progress.md` is the build log for tasks 01 through 20, including limits that later tasks removed. This manual is the description of the system those tasks produced.

#v(4mm)
#block(
  width: 100%,
  inset: 8pt,
  fill: rgb("#f4f7f5"),
  stroke: 0.4pt + rgb("#c5cdd6"),
)[
  #text(font: "Liberation Sans", size: 9pt)[
    The graph, the path checks, the review policy, and the runner protocol are the controls. The models propose objects. A person approves a plan before a worktree is edited, and approves again before a branch is pushed.
  ]
]
