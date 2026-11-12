"""Normalize existing Native send evidence without authorizing a retry.

This projection is for callers that need the shared tri-state contract. The
legacy wire flags/receipt remain untouched; accepted still requires the caller's
existing binding and projection checks before passing server_confirmed=True.
"""
from ...core.contracts import SendOutcome, SendResult


def send_result(ok, server_confirmed, submitted, code, receipt=None, parts=()):
    outcome = (SendOutcome.ACCEPTED if ok and server_confirmed else
               SendOutcome.UNKNOWN if submitted or server_confirmed else SendOutcome.REJECTED)
    return SendResult(outcome, receipt, code, tuple(parts))
