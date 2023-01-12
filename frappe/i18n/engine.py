import os
import gettext

import frappe


def get_translator(lang: str, localedir: str | None = "locale", context: bool | None = False):
    t = gettext.translation("messages", localedir=localedir, languages=(lang,), fallback=True)

    if context:
        return t.pgettext

    return t.gettext


def f(msg: str, context: str = None, lang: str = "en"):
    from frappe import as_unicode
    from frappe.utils import is_html, strip_html_tags

    if not lang:
        # TODO: change to en
        lang = "sl"

    msg = as_unicode(msg).strip()

    if is_html(msg):
        msg = strip_html_tags(msg)

    apps = frappe.get_all_apps()

    for app in apps:
        app_path = frappe.get_pymodule_path(app)
        locale_path = os.path.join(app_path, "locale")

        t = get_translator(lang, localedir=locale_path, context=context is not None)

        if context:
            r = t(context, msg)
            if r:
                return r

        r = t(msg)
        if r:
            return r

    return msg
