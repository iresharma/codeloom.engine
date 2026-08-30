from runtime.commands import files as _files  # noqa: F401
from runtime.commands import git as _git  # noqa: F401
from runtime.commands import lifecycle as _lifecycle  # noqa: F401
from runtime.commands.register import HANDLERS, handles

__all__ = ["HANDLERS", "handles"]
