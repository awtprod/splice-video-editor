"""Open DaVinci Resolve projects: ``.drp`` exports and projects in a disk library.

Reading only; the model keeps Resolve's ids (``resolveItemId``, ``resolveMediaId``,
``resolveSource``) so a later writer can carry untouched objects over from the original.
"""
from .source import ResolveError, list_library, load_drp, load_library, scratch_folder
from . import importer

__all__ = ['ResolveError', 'list_library', 'open_drp', 'open_library_project']


def open_drp(data, index=0, link_media=None):
    """Splice's project for timeline ``index`` of a ``.drp`` (bytes)."""
    model = importer.read(load_drp(data))
    return importer.to_splice(model, index, link_media, {'format': 'drp'})


def open_library_project(path, index=0, link_media=None, scratch=None):
    """Splice's project for timeline ``index`` of a disk library ``Project.db`` (read via a snapshot)."""
    model = importer.read(load_library(path, scratch if scratch is not None else scratch_folder()))
    return importer.to_splice(model, index, link_media, {'format': 'library', 'path': str(path)})

