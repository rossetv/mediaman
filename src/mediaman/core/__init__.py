"""Ring 0: pure functions, stdlib only, never imports from other parts of mediaman/.

Anything in here is unconditionally safe to import from any layer of the
application — routes, services, background tasks, tests.  Modules in this
package must satisfy all three invariants:

1. **No I/O.** No network calls, no file-system reads, no database queries,
   no subprocess invocations.
2. **No mediaman imports.** ``from mediaman.core import …`` is the only
   intra-package import ever permitted.  Cross-imports from
   ``mediaman.services``, ``mediaman.web``, etc. are forbidden.
3. **No shared mutable state.**  Module-level singletons are fine only when
   they are effectively constant (compiled regexes, frozen sets, etc.).

The practical consequence is that you can ``import mediaman.core.anything``
at the top of any file without worrying about circular imports, missing
config, or slow start-up side-effects.
"""
