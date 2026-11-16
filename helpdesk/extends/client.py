# Copyright (c) 2022, Frappe Technologies Pvt. Ltd. and Contributors
# MIT License. See license.txt

# Bitsol: legacy listManager API. Upstream removed this module in Dec 2024
# (41539409f); it is kept because the Bitsol ERP mobile app lists service
# requests through `helpdesk.extends.client.get_list`. Not used by the desk SPA.

import importlib
import math

import frappe
from frappe import _
from frappe.model.document import get_controller
from frappe.utils import cint, get_user_info_for_avatar, sbool
from frappe.utils.caching import redis_cache

from helpdesk.utils import check_permissions

from .doc import apply_sort


@frappe.whitelist()
def get_list(
    doctype: str | None = None,
    fields: str | list | None = None,
    filters: str | dict | list | None = None,
    order_by: str | None = None,
    start: int | str | None = 0,
    limit: int | str | None = None,
    group_by: str | None = None,
    parent: str | None = None,
    debug: bool | int | str = False,
):
    check_allowed(doctype)
    check_permissions(doctype, parent)

    fields = _parse_json_arg(fields)
    filters = _parse_json_arg(filters)
    debug = _debug_flag(debug)

    query = frappe.qb.get_query(
        table=doctype,
        fields=fields,
        filters=filters,
        # v16 query engine throws TypeError unless these are real ints;
        # HTTP callers (mobile app) send them as strings.
        offset=cint(start) or None,
        limit=cint(limit) or None,
        group_by=group_by or None,
        order_by=order_by or None,
        **_permission_kwargs(parent),
    )

    query = apply_custom_filters(doctype, query)
    query = apply_hook(doctype, query)
    query = apply_sort(doctype, order_by, query)

    if not fields:
        query = apply_custom_select(doctype, query)

    res = query.run(as_dict=True, debug=debug)
    res = transform_avatar(doctype, res)
    res = transform_assign(res)
    return res


@frappe.whitelist()
def get_list_meta(
    doctype: str | None = None,
    filters: str | dict | list | None = None,
    order_by: str | None = None,
    start: int | str | None = 0,
    limit: int | str | None = None,
    group_by: str | None = None,
    parent: str | None = None,
    debug: bool | int | str = False,
):
    check_allowed(doctype)
    check_permissions(doctype, parent)

    filters = _parse_json_arg(filters)
    debug = _debug_flag(debug)
    start = cint(start)
    limit = cint(limit)

    query = frappe.qb.get_query(
        table=doctype,
        filters=filters,
        group_by=group_by or None,
        # Aggregate only (no plain column, no ORDER BY) so the count is valid
        # under ONLY_FULL_GROUP_BY and on Postgres; `order_by` is irrelevant here.
        fields=[{"COUNT": "*", "as": "total_count"}],
        **_permission_kwargs(parent),
    )

    query = apply_custom_filters(doctype, query)
    query = apply_hook(doctype, query)

    res = query.run(as_dict=True, debug=debug)
    if group_by:
        # one row per group, i.e. as many rows as `get_list` returns
        total_count = len(res)
    else:
        total_count = cint(res[0].total_count) if res else 0
    total_pages = math.ceil(total_count / limit) if limit else 1
    current_page = start // limit + 1 if start and limit else 1
    has_next_page = current_page < total_pages
    has_previous_page = current_page > 1
    start_from = start + 1
    end_at = start + limit if limit else total_count

    if end_at > total_count:
        end_at = total_count

    return {
        "total_count": total_count,
        "total_pages": total_pages,
        "current_page": current_page,
        "has_next_page": has_next_page,
        "has_previous_page": has_previous_page,
        "start_from": start_from,
        "end_at": end_at,
    }


def _parse_json_arg(value):
    """
    `fields` / `filters` arrive as JSON strings when the caller posts them
    form-encoded. The v16 query engine would treat such a string as a field
    name / a `name` filter, so decode it first.
    """
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        if stripped[0] in "[{":
            return frappe.parse_json(stripped)
    return value


def _debug_flag(debug) -> bool:
    """`debug` is client controlled; only honour it in developer mode."""
    return bool(sbool(debug) is True or cint(debug)) and bool(
        frappe.conf.developer_mode
    )


def _permission_kwargs(parent: str | None = None) -> dict:
    """
    `frappe.qb.get_query` defaults to `ignore_permissions=True` on v16, which
    would let anyone with doctype-level read (e.g. every Employee on HD Ticket)
    list every row. By default the v16 query engine is therefore asked to
    enforce permission query conditions, User Permissions and field-level
    permissions, so non-agents only get the tickets they may see.

    The v15 behaviour (only the doctype-level check in `check_permissions`,
    no row filtering) can be restored per site with
    `"helpdesk_mobile_list_ignore_permissions": 1` in site_config.json.
    """
    if frappe.conf.get("helpdesk_mobile_list_ignore_permissions"):
        return {}
    kwargs = {"ignore_permissions": False}
    if parent:
        kwargs["parent_doctype"] = parent
    return kwargs


def apply_custom_filters(doctype: str, query):
    """
    Apply custom filters to query
    """
    controller = get_controller(doctype)

    if hasattr(controller, "get_list_filters"):
        return_value = controller.get_list_filters(query)
        if return_value is not None:
            query = return_value

    return query


def apply_custom_select(doctype: str, query):
    """
    Apply custom select logic to query
    """
    controller = get_controller(doctype)

    if hasattr(controller, "get_list_select"):
        return_value = controller.get_list_select(query)
        if return_value is not None:
            query = return_value

    return query


def apply_hook(doctype: str, query):
    """
    Apply hooks to query
    """
    try:
        _module_path = "helpdesk.helpdesk.hooks." + doctype.lower()
        # nosemgrep
        _module = importlib.import_module(_module_path)
        _class = getattr(_module, doctype)
        _function = getattr(_class, "get_list_filters")
        return _function(query)
    except Exception:
        return query


def transform_avatar(doctype: str, r):
    m = frappe.get_meta(doctype)
    f = [i for i in m.fields if i.fieldtype == "Link" and i.options == "User"]
    for i in f:
        for j in r:
            if j.get(i.fieldname):
                j[i.fieldname] = get_user_info_for_avatar(j[i.fieldname])
    return r


def transform_assign(r):
    for row in r:
        if assign := row.get("_assign"):
            j = frappe.parse_json(assign)
            if len(j) < 1:
                continue
            row["assignee"] = get_user_info_for_avatar(j.pop())
    return r


@redis_cache()
def check_allowed(doctype: str):
    """
    Allow only `Helpdesk` doctypes. This is to prevent users from accessing
    other doctypes.

    :param doctype: Doctype name
    """
    allowed = ["Contact"]
    if not (doctype in allowed or frappe.get_meta(doctype).module == "Helpdesk"):
        text = _("You are not allowed to access {0}").format(doctype)
        frappe.throw(text, frappe.PermissionError)
