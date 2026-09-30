"""How risky is clicking this element, or typing into that field?

The browser tools ask these functions for the permission level of each call:

    READ_ONLY / LOW_RISK  -> runs      (links, "Search", "Next", typing a search query)
    CONFIRM               -> asks you  (Buy, Order, Submit, Send, Sign in, Delete, other fields)
    SENSITIVE             -> refused   (Pay, Transfer, password or card fields, card numbers)

Typed text is checked too: something password-like ("Hunter2pass") always asks, even
for a search box, because a small model may pick the wrong field.

Matching looks at everything that describes the element: its visible text, label,
name/id, type and the form it submits. When in doubt, the higher level wins.
"""

import re

from app.tools.base import PermissionLevel

# Money leaving your account: never done by ARTHUR, even with confirmation.
_SENSITIVE_ACTION = re.compile(
    r"\b(pay|pay now|payment|transfer|withdraw|deposit|send money|wire|donate now)\b", re.I
)
# Actions with consequences: ARTHUR asks first.
_RISKY_ACTION = re.compile(
    r"\b(buy|purchase|order|checkout|check out|add to (cart|basket|bag)|place order|book( now)?|"
    r"reserve|subscribe|submit|send|post|publish|share|reply|comment|delete|remove|cancel|"
    r"unsubscribe|sign ?in|log ?in|sign ?up|register|create account|confirm|accept|agree|"
    r"apply|upload|save changes|update|install|download|donate|sponsor)\b",
    re.I,
)
_SENSITIVE_FIELD = re.compile(
    r"password|passcode|passwd|\bpin\b|otp|one[- ]time|cvv|cvc|card[- _]?(number|no)|"
    r"\bcc[-_]|iban|account[- _]?number|routing|sort[- _]?code|ssn|social security|"
    r"security code|expir",
    re.I,
)
# What is being TYPED can give it away too - the model may pick the wrong field.
_CARD_NUMBER = re.compile(r"(?:\d[ -]?){12,19}")
_PASSWORD_LIKE = re.compile(r"^(?=.*[A-Za-z])(?=.*\d)\S{6,}$")  # one "word" of letters+digits
_SEARCH_FIELD = re.compile(r"search|query|\bq\b|find|keyword|look ?up", re.I)


def click_level(element: dict) -> PermissionLevel:
    description = _describe(element)
    if _SENSITIVE_ACTION.search(description):
        return PermissionLevel.SENSITIVE
    if element.get("kind") == "submit" and not _SEARCH_FIELD.search(description):
        return PermissionLevel.CONFIRM  # submitting a (non-search) form
    if _RISKY_ACTION.search(description):
        return PermissionLevel.CONFIRM
    return PermissionLevel.LOW_RISK


def type_level(field: dict, *, submit: bool, text: str = "") -> PermissionLevel:
    description = _describe(field)
    if field.get("type") == "password" or _SENSITIVE_FIELD.search(description):
        return PermissionLevel.SENSITIVE
    if _CARD_NUMBER.search(text):
        return PermissionLevel.SENSITIVE
    if _PASSWORD_LIKE.match(text.strip()):
        return PermissionLevel.CONFIRM  # maybe a password in the wrong field: let the user see it
    if field.get("type") == "search" or _SEARCH_FIELD.search(description):
        return PermissionLevel.LOW_RISK
    return PermissionLevel.CONFIRM  # personal data in a form: the user decides


def _describe(element: dict) -> str:
    keys = ("text", "label", "name", "id_attr", "type", "placeholder", "autocomplete", "form")
    return " ".join(str(element.get(k) or "") for k in keys)
