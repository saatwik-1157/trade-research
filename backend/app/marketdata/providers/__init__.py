"""Provider adapters.

Each wraps an existing fetcher rather than reimplementing one. The toolkit
modules in `tools/` are called, not copied, so every command in README.md and
NIGHTLY.md keeps working against the same source and a fix lands in one place.
"""
