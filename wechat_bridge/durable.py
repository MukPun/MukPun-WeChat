"""Compatibility exports for the durable pending-message spool."""
from .storage.durable import Spool
from .storage.state import State, StateError

__all__ = ['Spool', 'State', 'StateError']
