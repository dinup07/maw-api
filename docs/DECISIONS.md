# Implementation decisions

Choices made where the spec is silent or ambiguous. Newest last.

1. **Repository root is the project root.** The spec's §4 tree is rooted at `maw/`; this repo
   (`maw-api`) holds that tree directly at its root rather than in a nested `maw/` folder.
2. **Python 3.12 is pinned via `.python-version`**, to match the `python:3.12-slim` runtime image,
   although `requires-python` stays `>=3.12`.
