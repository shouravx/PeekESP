"""The version this build reports.

Kept in step with the VERSION file at the repository root by windows/build.py,
which refuses to build when the two disagree. A build claiming a version it is
not is worse than one claiming none, because the update check believes it.
"""

__version__ = "1.1.0"
