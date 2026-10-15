# Implementation decisions

Choices made where the spec is silent or ambiguous. Newest last.

1. **Repository root is the project root.** The spec's §4 tree is rooted at `maw/`; this repo
   (`maw-api`) holds that tree directly at its root rather than in a nested `maw/` folder.
2. **Python 3.12 is pinned via `.python-version`**, to match the `python:3.12-slim` runtime image,
   although `requires-python` stays `>=3.12`.
3. **Reference `input_mapping` maps by node type.** The compat table maps it to `inputs`, but
   native `mcp` and `transform` nodes have no `inputs`: on `mcp_node` it becomes `arguments`, on
   `enrichment_node` it becomes `mapping` (unless one is already present).
4. **Request fields next to `workflow_config` are ignored by `normalize`.** `input_prompt`,
   `session_id`, `async` and `user_id` belong to the run request (handled by `RunRequest`), so a
   reference *request* file also validates as a workflow. Any other stray top-level key is dropped
   with a warning; a top-level `id` is used as the workflow id if `workflow_config` has none.
5. **`{x}` → `{{ x }}` conversion applies to `user_message` only**, as the compat table says.
   Placeholders must look like a path (`[A-Za-z_][A-Za-z0-9_.]*`), so literal JSON in a prompt is
   left alone.
6. **Unsupported reference `config` keys are dropped with a warning** (only `recursion_limit` and
   `max_iterations` have native equivalents).
7. **The function registry and built-ins were built in Phase 1** (the spec schedules them for
   Phase 2) because `maw validate` needs the function names to check references.
