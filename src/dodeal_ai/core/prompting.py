"""Prompt builder — foundation_report.md §6.

Assembles system prompt + injected context + task, server-side, from the
versioned files in /prompts. Caller input is treated as DATA, never as
instructions (prompt-injection boundary).
"""
