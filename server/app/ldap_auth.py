"""LDAP / Active Directory authentication.

Returns a dict of attributes on success, or None on failure / not configured.
Never raises (caller falls back to local auth on None).
"""
from __future__ import annotations

import logging
from typing import Optional

from .config import settings

log = logging.getLogger(__name__)


def is_configured() -> bool:
    return bool(settings.ldap_url and settings.ldap_user_search_base)


def authenticate(username: str, password: str) -> Optional[dict]:
    if not is_configured() or not username or not password:
        return None
    try:
        import ldap3
        from ldap3.utils.conv import escape_filter_chars
    except ImportError:
        log.warning("ldap3 not installed; LDAP login disabled")
        return None

    try:
        server = ldap3.Server(settings.ldap_url, use_ssl=settings.ldap_use_ssl, get_info=ldap3.NONE)
        conn = ldap3.Connection(
            server,
            user=settings.ldap_bind_dn or None,
            password=settings.ldap_bind_password or None,
            auto_bind=True,
            receive_timeout=10,
        )
        user_filter = settings.ldap_user_filter.replace(
            "{username}", escape_filter_chars(username)
        )
        conn.search(
            settings.ldap_user_search_base,
            user_filter,
            attributes=["memberOf", "displayName", "mail", "cn"],
        )
        if not conn.entries:
            conn.unbind()
            return None
        entry = conn.entries[0]
        user_dn = entry.entry_dn
        member_of = [str(g) for g in (entry.memberOf.values if hasattr(entry, "memberOf") else [])]
        display_name = (
            str(entry.displayName.value)
            if hasattr(entry, "displayName") and entry.displayName.value
            else username
        )
        conn.unbind()

        # Bind as the user to verify their password.
        user_conn = ldap3.Connection(server, user=user_dn, password=password, receive_timeout=10)
        if not user_conn.bind():
            return None
        user_conn.unbind()

        if settings.ldap_admin_group_dn:
            wanted = settings.ldap_admin_group_dn.lower()
            if not any(g.lower() == wanted for g in member_of):
                return None

        return {"display_name": display_name, "groups": member_of}
    except Exception as e:
        log.warning("LDAP auth error for %s: %s", username, e)
        return None
