"""Focused ASGI/HTTP middleware modules used by the FastAPI app.

Each sub-module owns a single, narrowly-scoped middleware class plus its
direct helpers. The orchestrator that wires them together lives in
:mod:`mediaman.web` so the app factory has a single import target.

NOTE on ``BaseHTTPMiddleware`` (Starlette):

Starlette's docs explicitly recommend pure-ASGI middleware over
:class:`starlette.middleware.base.BaseHTTPMiddleware` for production
use.  ``BaseHTTPMiddleware`` buffers request/response bodies in memory
and runs the inner app on a detached task, which complicates streaming,
timeouts, and cancellation semantics.  Most middleware in this package
still use it because their behaviour is well-tested against the
request/response object model and the migration risk outweighs the
theoretical streaming benefit for these short, header-centric paths
(no body inspection, no streaming responses).

:class:`mediaman.web.middleware.body_size.BodySizeLimitMiddleware` is
pure ASGI by necessity — it must enforce a cap as bytes stream in.
Future migrations of the others should preserve every existing
behaviour and test before flipping over.
"""
