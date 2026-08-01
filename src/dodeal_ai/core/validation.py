"""Response validation — foundation_report.md §6.

Validates every model/tool output against its schema in /schemas before the
value is used or returned. Malformed output never surfaces. Called by tools/
(tool responses) and units/ (LLM output).
"""
