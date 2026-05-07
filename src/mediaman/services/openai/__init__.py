"""OpenAI client — personalised media recommendation generation (optional feature).

Sub-packages: ``client`` (OpenAI API wrapper), ``recommendations`` (prompt
assembly, LLM-output validation, persistence, and refresh-cooldown logic).
The feature is gated on the ``openai_api_key`` setting; when absent the
sub-package is never imported.

Allowed dependencies: ``mediaman.services.infra.http``, ``mediaman.crypto``,
``mediaman.db``; ``openai`` third-party package (optional).

Forbidden patterns: do not let raw LLM output reach any other layer without
passing through the ``_validate_llm_string`` filter — prompt-injection defence
lives in ``recommendations.prompts``.
"""
