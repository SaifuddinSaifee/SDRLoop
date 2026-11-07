# Decisions

1. **Fixed pipeline, one free agent (research).** Predictable cost, easy step-level tests.
2. **One LLM gateway (`llm.py`).** Retries, fallbacks, routing, budgets and spans in one place.
3. **Critic from a different family than the writer.** Claude writes; Qwen (Together) critiques; Llama (Together) qualifies.
4. **Facts validated in code.** A fact is kept only if its snippet is a substring of real tool output.
5. **Synthetic Evaboot fixture** until API access exists. One column-alias map to swap in a real export.
