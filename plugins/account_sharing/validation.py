"""Native identifier validation shared by account adapters."""

import json


def identity_key(*parts):
    return json.dumps(parts, ensure_ascii=False)


def identifier(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 256:
        raise ValueError('请提供明确的 Bot、功能、群和原生身份标识')
    return value
