"""Logging filter that masks anything looking like a voucher code."""

import logging
import re

VOUCHER_PARAM_RE = re.compile(r"(voucher=)[^&\s\"'<>]+", re.IGNORECASE)
MASK = "***"


def _code_regex():
    from django.conf import settings

    try:
        return re.compile(settings.VOUCHER_CODE_REGEX)
    except Exception:  # settings not configured or bad regex: still mask URL params
        return None


def mask_codes(text: str) -> str:
    text = VOUCHER_PARAM_RE.sub(r"\1" + MASK, text)
    regex = _code_regex()
    if regex is not None:
        text = regex.sub(MASK, text)
    return text


class MaskVoucherCodesFilter(logging.Filter):
    def filter(self, record):
        try:
            message = record.getMessage()
        except Exception:
            return True
        masked = mask_codes(message)
        if masked != message:
            record.msg = masked
            record.args = None
        return True
