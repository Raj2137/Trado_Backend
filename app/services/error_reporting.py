"""Useful exchange errors without credentials or signed request URLs."""

import re
from urllib.parse import quote, quote_plus, urlsplit, urlunsplit


def redact_error(error, sensitive_values=()):
    message = str(error)
    for value in sorted((str(value) for value in sensitive_values if value), key=len, reverse=True):
        for variant in (value, quote(value, safe=""), quote_plus(value, safe="")):
            message = message.replace(variant, "[REDACTED]")

    def clean_url(match):
        parsed = urlsplit(match.group(0))
        # Signed query strings and URL credentials are never useful in logs.
        host = parsed.netloc.rsplit("@", 1)[-1]
        return urlunsplit((parsed.scheme, host, parsed.path, "", ""))

    message = re.sub(r"https?://[^\s\"'<>]+", clean_url, message)
    message = re.sub(
        r'''(?i)(["']?(?:signature|x-mbx-apikey|api[_-]?key|secret(?:[_-]?key)?|password|token)["']?\s*[:=]\s*)(?:"[^"]*"|'[^']*'|[^&\s,}\]]+)''',
        r"\1[REDACTED]", message,
    )
    return message.replace("\r", " ").replace("\n", " ")[:1500]
