"""Every intake source. A new source is a module here that subclasses intake.base.Source and uses
@register; importing it below is all the wiring it needs."""
from . import local, practice  # noqa: F401
