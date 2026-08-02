"""Code shared by the skills and the worker.

Stdlib only, like the skills themselves. Skills run with their own directory as
cwd, so each one puts the repo root on sys.path before importing this:

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from skilllib import llm, notefmt

Nothing here reads the environment at import time. Configuration is passed in
explicitly (see llm.Config), because the test suite re-imports modules to pick
up patched environments and a cached shared module would freeze the first
test's config into every later one.
"""
