from django import template

register = template.Library()

_STATUS_CSS = {
    "queued": "badge-slate",
    "scheduled": "badge-slate",
    "sending": "badge-yellow",
    "sent": "badge-yellow",
    "delivered": "badge-green",
    "failed": "badge-red",
    "bounced": "badge-red",
    "complained": "badge-red",
    "captured": "badge-green",
}


@register.simple_tag
def status_badge(status: str) -> str:
    css = _STATUS_CSS.get(status, "badge-slate")
    label = status.capitalize() if status else "—"
    from django.utils.html import format_html

    return format_html('<span class="{}">{}</span>', css, label)
