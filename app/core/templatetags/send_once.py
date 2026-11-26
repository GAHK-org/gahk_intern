"""`{% send_token %}`: the hidden field that makes a form's send count once (core/send_once.py)."""

import uuid

from django import template
from django.utils.html import format_html
from django.utils.safestring import SafeString

from core.send_once import FIELD

register = template.Library()


@register.simple_tag
def send_token() -> SafeString:
    """A new token per form, never one per page: two forms on a page are two sends."""
    return format_html('<input type="hidden" name="{}" value="{}" data-send-token>', FIELD, uuid.uuid4())
