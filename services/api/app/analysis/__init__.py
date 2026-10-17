"""Analysis pipeline: the scan pass (`app.analysis.scan`) and the job runner
that orchestrates a pass end to end (`app.analysis.jobs`).

Deliberately no eager re-exports here: `app.analysis.jobs` depends on
`app.repository.ingest`, which itself imports `app.analysis.scan` — importing
that at package-init time would be a circular import. Import from the
submodule you need (`app.analysis.jobs`, `app.analysis.scan`) directly.
"""
