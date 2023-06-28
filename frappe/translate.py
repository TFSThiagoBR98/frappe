# Copyright (c) 2021, Frappe Technologies Pvt. Ltd. and Contributors
# License: MIT. See LICENSE
"""
	frappe.translate
	~~~~~~~~~~~~~~~~

	Translation tools for frappe
"""


import csv
import functools
import gettext
import itertools
import json
import operator
import os
import re

from collections import defaultdict
from contextlib import contextmanager, suppress
from datetime import datetime
from pathlib import Path

from pypika.terms import PseudoColumn

from babel.messages.catalog import Catalog
from babel.messages.extract import extract_from_dir, extract_python
from babel.messages.mofile import read_mo, write_mo
from babel.messages.pofile import read_po, write_po
from babel.messages.extract import DEFAULT_KEYWORDS

import frappe
from frappe.query_builder import DocType, Field
from frappe.utils import is_html, strip_html_tags, unique

TRANSLATE_PATTERN = re.compile(
	r"_\(\s*"  # starts with literal `_(`, ignore following whitespace/newlines
	# BEGIN: message search
	r"([\"']{,3})"  # start of message string identifier - allows: ', ", """, '''; 1st capture group
	r"(?P<message>((?!\1).)*)"  # Keep matching until string closing identifier is met which is same as 1st capture group
	r"\1"  # match exact string closing identifier
	# END: message search
	# BEGIN: python context search
	r"(\s*,\s*context\s*=\s*"  # capture `context=` with ignoring whitespace
	r"([\"'])"  # start of context string identifier; 5th capture group
	r"(?P<py_context>((?!\5).)*)"  # capture context string till closing id is found
	r"\5"  # match context string closure
	r")?"  # match 0 or 1 context strings
	# END: python context search
	# BEGIN: JS context search
	r"(\s*,\s*(.)*?\s*(,\s*"  # skip message format replacements: ["format", ...] | null | []
	r"([\"'])"  # start of context string; 11th capture group
	r"(?P<js_context>((?!\11).)*)"  # capture context string till closing id is found
	r"\11"  # match context string closure
	r")*"
	r")*"  # match one or more context string
	# END: JS context search
	r"\s*\)"  # Closing function call ignore leading whitespace/newlines
)
REPORT_TRANSLATE_PATTERN = re.compile('"([^:,^"]*):')
CSV_STRIP_WHITESPACE_PATTERN = re.compile(r"{\s?([0-9]+)\s?}")

DEFAULT_LANG = "en"
LOCALE_DIR = "locale"
MERGED_TRANSLATION_KEY = "merged_translations"
POT_FILE = "main.pot"
TRANSLATION_DOMAIN = "messages"
USER_TRANSLATION_KEY = "lang_user_translations"


def get_language(lang_list: list = None) -> str:
	"""Set `frappe.local.lang` from HTTP headers at beginning of request

	Order of priority for setting language:
	1. Form Dict => _lang
	2. Cookie => preferred_language (Non authorized user)
	3. Request Header => Accept-Language (Non authorized user)
	4. User document => language
	5. System Settings => language
	"""
	is_logged_in = frappe.session.user != "Guest"

	# fetch language from form_dict
	if frappe.form_dict._lang:
		language = get_lang_code(frappe.form_dict._lang or get_parent_language(frappe.form_dict._lang))
		if language:
			return language

	# use language set in User or System Settings if user is logged in
	if is_logged_in:
		return frappe.local.lang

	lang_set = set(lang_list or get_all_languages() or [])

	# fetch language from cookie
	preferred_language_cookie = get_preferred_language_cookie()

	if preferred_language_cookie:
		if preferred_language_cookie in lang_set:
			return preferred_language_cookie

		parent_language = get_parent_language(language)
		if parent_language in lang_set:
			return parent_language

	# fetch language from request headers
	accept_language = list(frappe.request.accept_languages.values())

	for language in accept_language:
		if language in lang_set:
			return language

		parent_language = get_parent_language(language)
		if parent_language in lang_set:
			return parent_language

	# fallback to language set in System Settings or "en"
	return frappe.db.get_default("lang") or "en"


@functools.lru_cache
def get_parent_language(lang: str) -> str:
	"""If the passed language is a variant, return its parent

	Eg:
	        1. zh-TW -> zh
	        2. sr-BA -> sr
	"""
	is_language_variant = "-" in lang
	if is_language_variant:
		return lang[: lang.index("-")]


def get_user_lang(user: str = None) -> str:
	"""Set frappe.local.lang from user preferences on session beginning or resumption"""
	user = user or frappe.session.user
	lang = frappe.cache.hget("lang", user)

	if not lang:
		# User.language => Session Defaults => frappe.local.lang => 'en'
		lang = (
			frappe.db.get_value("User", user, "language")
			or frappe.db.get_default("lang")
			or frappe.local.lang
			or "en"
		)

		frappe.cache.hset("lang", user, lang)

	return lang


def get_lang_code(lang: str) -> str | None:
	return frappe.db.get_value("Language", {"name": lang}) or frappe.db.get_value(
		"Language", {"language_name": lang}
	)


def set_default_language(lang):
	"""Set Global default language"""
	if frappe.db.get_default("lang") != lang:
		frappe.db.set_default("lang", lang)
	frappe.local.lang = lang


def get_lang_dict():
	"""Returns all languages in dict format, full name is the key e.g. `{"english":"en"}`"""
	return dict(
		frappe.get_all("Language", fields=["language_name", "name"], order_by="modified", as_list=True)
	)


def get_translator(lang: str, localedir: str | None = LOCALE_DIR, context: bool | None = False):
	t = gettext.translation(TRANSLATION_DOMAIN, localedir=localedir, languages=(lang,), fallback=True)

	if context:
		return t.pgettext

	return t.gettext


def new_catalog(app: str, locale: str | None = None) -> Catalog:
	def get_hook(hook, app):
		return frappe.get_hooks(hook, [None], app)[0]

	app_email = get_hook("app_email", app)
	return Catalog(
		locale=locale,
		domain="messages",
		msgid_bugs_address=app_email,
		language_team=app_email,
		copyright_holder=get_hook("app_publisher", app),
		last_translator=app_email,
		project=get_hook("app_title", app),
		creation_date=datetime.now(),
		revision_date=datetime.now(),
		fuzzy=False,
	)


def get_locales_dir(app: str) -> Path:
	return Path(frappe.get_app_path(app)) / LOCALE_DIR


def get_locales(app: str) -> list[str]:
	return [locale.name for locale in get_locales_dir(app).iterdir() if locale.is_dir()]


def get_po_path(app: str, locale: str | None = None) -> Path:
	return get_locales_dir(app) / locale / "LC_MESSAGES" / "messages.po"


def get_pot_path(app: str) -> Path:
	return get_locales_dir(app) / "main.pot"


def get_catalog(app: str, locale: str | None = None) -> Catalog:
	"""Returns a catatalog for the given app and locale"""
	po_path = get_po_path(app, locale) if locale else get_pot_path(app)

	if not po_path.exists():
		return new_catalog(app, locale)

	with open(po_path, "rb") as f:
		return read_po(f)


def write_catalog(app: str, catalog: Catalog, locale: str | None = None) -> Path:
	"""Writes a catalog to the given app and locale"""
	po_path = get_po_path(app, locale) if locale else get_pot_path(app)

	if not po_path.parent.exists():
		po_path.parent.mkdir(parents=True)

	with open(po_path, "wb") as f:
		write_po(f, catalog, sort_output=True, ignore_obsolete=True)

	return po_path


def write_binary(app: str, catalog: Catalog, locale: str) -> Path:
	po_path = get_po_path(app, locale)
	mo_path = po_path.with_suffix(".mo")
	with open(mo_path, "wb") as mo_file:
		write_mo(mo_file, catalog)

	return mo_path


def generate_pot(target_app: str | None = None):
	"""
	Generate a POT (PO template) file. This file will contain only messages IDs.
	https://en.wikipedia.org/wiki/Gettext

	:param target_app: If specified, limit to `app`
	"""

	def directory_filter(dirpath: str | os.PathLike[str]) -> bool:
		if "public/dist" in dirpath:
			return False

		subdir = os.path.basename(dirpath)
		return not (subdir.startswith(".") or subdir.startswith("_"))

	apps = [target_app] if target_app else frappe.get_all_apps(with_internal_apps=True)
	method_map = [
		("**.py", "frappe.translate.babel_extract_python"),
		("**.js", "frappe.translate.babel_extract_javascript"),
		("**/doctype/*/*.json", "frappe.translate.babel_extract_doctype_json"),
		("**/form_tour/*/*.json", "frappe.translate.babel_extract_form_tour_json"),
		("**/workspace/*/*.json", "frappe.translate.babel_extract_workspace_json"),
		("**/public/**/*.html", "frappe.translate.babel_extract_jinja"),
		("**/templates/**/*.html", "frappe.translate.babel_extract_jinja"),
		("**/www/**/*.html", "frappe.translate.babel_extract_jinja"),
	]

	for app in apps:
		app_path = frappe.get_pymodule_path(app)
		catalog = get_catalog(app)

		method_map.extend(get_extra_include_js_files(app))

		for filename, lineno, message, comments, context in extract_from_dir(
			app_path, method_map, directory_filter=directory_filter
		):
			if not message:
				continue

			catalog.add(message, locations=[(filename, lineno)], auto_comments=comments, context=context)

		## Load Message Injectors
		messages = []
		modules = [frappe.unscrub(m) for m in frappe.local.app_modules[app]]

		# doctypes
		if modules:
			if isinstance(modules, str):
				modules = [modules]
			filtered_doctypes = (
				frappe.qb.from_("DocType").where(Field("module").isin(modules)).select("name").run(pluck=True)
			)
			for name in filtered_doctypes:
				messages.extend(extract_messages_from_doctype(name))

			# reports
			report = DocType("Report")
			doctype = DocType("DocType")
			names = (
				frappe.qb.from_(doctype)
				.from_(report)
				.where((report.ref_doctype == doctype.name) & doctype.module.isin(modules))
				.select(report.name)
				.run(pluck=True)
			)
			for name in names:
				messages.append((None, name))
				messages.extend(extract_messages_from_report(name))
				for i in messages:
					if not isinstance(i, tuple):
						raise Exception

		# workflow based on app.hooks.fixtures
		messages.extend(extract_messages_from_workflow(app_name=app))

		# custom fields based on app.hooks.fixtures
		messages.extend(extract_messages_from_custom_fields(app_name=app))

		# app extras
		messages.extend(extract_messages_from_extras())

		# messages from navbar settings
		messages.extend(extract_messages_from_navbar())

		messages = deduplicate_messages(messages)

		for app_message in messages:
			if not app_message:
				continue

			context = None
			lineno = 1
			if len(app_message) == 2:
				path, message = app_message
			elif len(app_message) == 3:
				path, message, lineno = app_message
			elif len(app_message) == 4:
				path, message, context, lineno = app_message
			else:
				continue

			if not message:
				continue

			catalog.add(message, auto_comments=['{}:{}'.format(path, lineno)], context=context)

		pot_path = write_catalog(app, catalog)
		print(f"POT file created at {pot_path}")


def new_po(locale, target_app: str | None = None):
	apps = [target_app] if target_app else frappe.get_all_apps(with_internal_apps=True)

	for target_app in apps:
		po_path = get_po_path(target_app, locale)
		if os.path.exists(po_path):
			print(f"{po_path} exists. Skipping")
			continue

		pot_catalog = get_catalog(target_app)
		pot_catalog.locale = locale
		po_path = write_catalog(target_app, pot_catalog, locale)

		print(f"PO file created_at {po_path}")
		print(
			"You will need to add the language in frappe/geo/languages.json, if you haven't done it already."
		)


def compile(target_app: str | None = None, locale: str | None = None):
	apps = [target_app] if target_app else frappe.get_all_apps(with_internal_apps=True)

	for app in apps:
		locales = [locale] if locale else get_locales(app)
		for locale in locales:
			catalog = get_catalog(app, locale)
			mo_path = write_binary(app, catalog, locale)
			print(f"MO file created at {mo_path}")


def update_po(target_app: str | None = None, locale: str | None = None):
	"""
	Add keys to available PO files, from POT file. This could be used to keep
	track of available keys, and missing translations

	:param target_app: Limit operation to `app`, if specified
	"""
	apps = [target_app] if target_app else frappe.get_all_apps(with_internal_apps=True)

	for app in apps:
		locales = [locale] if locale else get_locales(app)
		pot_catalog = get_catalog(app)
		for locale in locales:
			po_catalog = get_catalog(app, locale)
			po_catalog.update(pot_catalog)
			po_path = write_catalog(app, po_catalog, locale)
			print(f"PO file modified at {po_path}")


def migrate(app: str | None = None, locale: str | None = None):
	apps = [app] if app else frappe.get_all_apps(with_internal_apps=True)

	for app in apps:
		if locale:
			csv_to_po(app, locale)
		else:
			app_path = Path(frappe.get_app_path(app))
			for filename in (app_path / "translations").iterdir():
				if filename.suffix != ".csv":
					continue
				csv_to_po(app, filename.stem)


def csv_to_po(app: str, locale: str):
	csv_file = Path(frappe.get_app_path(app)) / "translations" / f"{locale.replace('_', '-')}.csv"
	locale = locale.replace("-", "_")
	if not csv_file.exists():
		return

	catalog: Catalog = get_catalog(app)
	msgid_context_map = defaultdict(list)
	for message in catalog:
		msgid_context_map[message.id].append(message.context)

	with open(csv_file) as f:
		for row in csv.reader(f):
			if len(row) < 2:
				continue

			msgid = escape_percent(row[0])
			msgstr = escape_percent(row[1])
			msgctxt = row[2] if len(row) >= 3 else None

			if not msgctxt:
				# if old context is not defined, add msgstr to all contexts
				for context in msgid_context_map.get(msgid, []):
					if message := catalog.get(msgid, context):
						message.string = msgstr
			elif message := catalog.get(msgid, msgctxt):
				message.string = msgstr

	po_path = write_catalog(app, catalog, locale)
	print(f"PO file created at {po_path}")


def f(msg: str, context: str = None, lang: str = DEFAULT_LANG) -> str:
	"""
	Method to translate a string

	:param msg: Key to translate
	:param context: Translation context
	:param lang: Language to fetch
	:return: Translated string. Could be original string
	"""
	from frappe import as_unicode
	from frappe.utils import is_html, strip_html_tags

	if not lang:
		lang = DEFAULT_LANG

	msg = as_unicode(msg).strip()

	if is_html(msg):
		msg = strip_html_tags(msg)

	apps = frappe.get_all_apps(with_internal_apps=True)

	for app in apps:
		app_path = frappe.get_pymodule_path(app)
		locale_path = os.path.join(app_path, LOCALE_DIR)
		has_context = context is not None

		if has_context:
			t = get_translator(lang.replace("-", "_"), localedir=locale_path, context=has_context)
			r = t(context, msg)
			if r != msg:
				return r

		t = get_translator(lang.replace("-", "_"), localedir=locale_path, context=False)
		r = t(msg)

		if r != msg:
			return r

	return msg


def get_messages_for_boot():
	"""
	Return all message translations that are required on boot
	"""
	messages = get_all_translations(frappe.local.lang)
	messages.update(get_dict_from_hooks("boot", None))

	return messages


def get_dict_from_hooks(fortype: str, name: str) -> dict[str, str]:
	"""
	Get and run a custom translator method from hooks for item.

	Hook example:
	```
	get_translated_dict = {
	        ("doctype", "Global Defaults"): "frappe.geo.country_info.get_translated_dict",
	}
	```

	:param fortype: Item type. eg: doctype
	:param name: Item name. eg: User
	:return: Dictionary with translated messages
	"""
	translated_dict = {}
	hooks = frappe.get_hooks("get_translated_dict")

	for (hook_fortype, fortype_name) in hooks:
		if hook_fortype == fortype and fortype_name == name:
			for method in hooks[(hook_fortype, fortype_name)]:
				translated_dict.update(frappe.get_attr(method)())

	return translated_dict


def make_dict_from_messages(messages, full_dict=None, load_user_translation=True):
	"""Returns translated messages as a dict in Language specified in `frappe.local.lang`

	:param messages: List of untranslated messages
	"""
	out = {}
	if full_dict is None:
		if load_user_translation:
			full_dict = get_all_translations(frappe.local.lang)
		else:
			full_dict = get_translations_from_apps(frappe.local.lang)

	for m in messages:
		if m[1] in full_dict:
			out[m[1]] = full_dict[m[1]]
		# check if msg with context as key exist eg. msg:context
		if len(m) > 2 and m[2]:
			key = m[1] + ":" + m[2]
			if full_dict.get(key):
				out[key] = full_dict[key]

	return out


def get_all_translations(lang: str) -> dict[str, str]:
	"""
	Load and return the entire translations dictionary for a language from apps
	+ user translations.

	:param lang: Language Code, e.g. `hi`
	:return: dictionary of key and value
	"""
	if not lang:
		return {}

	def t():
		all_translations = get_translations_from_apps(lang)
		with suppress(Exception):
			# get user specific translation data
			user_translations = get_user_translations(lang)
			all_translations.update(user_translations)

		return all_translations

	try:
		return frappe.cache.hget(MERGED_TRANSLATION_KEY, lang, generator=t)
	except Exception:
		# People mistakenly call translation function on global variables where
		# locals are not initialized, translations don't make much sense there
		return {}


def get_translations_from_apps(lang, apps=None):
	"""
	Combine all translations from `.mo` files in all `apps`. For derivative
	languages (es-GT), take translations from the base language (es) and then
	update translations from the child (es-GT)
	"""
	if not lang or lang == DEFAULT_LANG:
		return {}

	translations = {}

	for app in apps or frappe.get_all_apps(with_internal_apps=True):
		app_path = frappe.get_pymodule_path(app)
		localedir = os.path.join(app_path, LOCALE_DIR)
		mo_files = gettext.find(TRANSLATION_DOMAIN, localedir, (lang.replace("-", "_"),), True)

		for file in mo_files:
			with open(file, "rb") as f:
				po = read_mo(f)
				for m in po:
					translations[m.id] = m.string

	return translations

def get_user_translations(lang: str) -> dict[str, str]:
	"""
	Get translations from db, created by user

	:param lang: language to fetch
	:return: translation key/value
	"""
	if not frappe.db:
		frappe.connect()

	def f():
		user_translations = {}
		translations = frappe.get_all(
			"Translation",
			fields=["source_text", "translated_text", "context"],
			filters={"language": lang},
		)

		for t in translations:
			key = t.source_text
			value = t.translated_text
			if t.context:
				key += ":" + t.context
			user_translations[key] = value

		return user_translations

	return frappe.cache.hget(USER_TRANSLATION_KEY, lang, generator=f)


def clear_cache():
	"""Clear all translation assets from :meth:`frappe.cache`"""
	frappe.cache.delete_key("langinfo")

	# clear translations saved in boot cache
	frappe.cache.delete_key("bootinfo")
	frappe.cache.delete_key("translation_assets")
	frappe.cache.delete_key(USER_TRANSLATION_KEY)
	frappe.cache.delete_key(MERGED_TRANSLATION_KEY)


def is_translatable(m):
	if (
		re.search("[a-zA-Z]", m)
		and not m.startswith("fa fa-")
		and not m.endswith("px")
		and not m.startswith("eval:")
	):
		return True
	return False


def add_line_number(messages, code):
	ret = []
	messages = sorted(messages, key=lambda x: x[0])
	newlines = [m.start() for m in re.compile(r"\n").finditer(code)]
	line = 1
	newline_i = 0
	for pos, message, context in messages:
		while newline_i < len(newlines) and pos > newlines[newline_i]:
			line += 1
			newline_i += 1
		ret.append([line, message, context])
	return ret


def send_translations(translation_dict):
	"""Append translated dict in `frappe.local.response`"""
	if "__messages" not in frappe.local.response:
		frappe.local.response["__messages"] = {}

	frappe.local.response["__messages"].update(translation_dict)


def deduplicate_messages(messages):
	ret = []
	op = operator.itemgetter(1)
	messages = sorted(messages, key=op)
	for k, g in itertools.groupby(messages, op):
		ret.append(next(g))
	return ret


def escape_percent(s: str):
	return s.replace("%", "&#37;")


@frappe.whitelist()
def update_translations_for_source(source=None, translation_dict=None):
	if not (source and translation_dict):
		return

	translation_dict = json.loads(translation_dict)

	if is_html(source):
		source = strip_html_tags(source)

	# for existing records
	translation_records = frappe.db.get_values(
		"Translation", {"source_text": source}, ["name", "language"], as_dict=1
	)
	for d in translation_records:
		if translation_dict.get(d.language, None):
			doc = frappe.get_doc("Translation", d.name)
			doc.translated_text = translation_dict.get(d.language)
			doc.save()
			# done with this lang value
			translation_dict.pop(d.language)
		else:
			frappe.delete_doc("Translation", d.name)

	# remaining values are to be inserted
	for lang, translated_text in translation_dict.items():
		doc = frappe.new_doc("Translation")
		doc.language = lang
		doc.source_text = source
		doc.translated_text = translated_text
		doc.save()

	return translation_records


@frappe.whitelist()
def get_translations(source_text):
	if is_html(source_text):
		source_text = strip_html_tags(source_text)

	return frappe.db.get_list(
		"Translation",
		fields=["name", "language", "translated_text as translation"],
		filters={"source_text": source_text},
	)


@frappe.whitelist()
def get_messages(language, start=0, page_length=100, search_text=""):
	from frappe.frappeclient import FrappeClient

	translator = FrappeClient(get_translator_url())
	translated_dict = translator.post_api(
		"translator.api.get_strings_for_translation", params=locals()
	)

	return translated_dict

def babel_extract_jinja(fileobj, keywords, comment_tags, options):
	from jinja2.ext import babel_extract

	# We use `__` as our translation function
	keywords = DEFAULT_KEYWORDS + {
		'__': None,
	}

	for lineno, funcname, messages, comments in babel_extract(
		fileobj, keywords, comment_tags, options
	):
		# `funcname` here will be `__` which is our translation function. We
		# have to convert it back to usual function names
		funcname = "gettext"

		if isinstance(messages, tuple):
			if len(messages) == 3:
				funcname = "pgettext"
				messages = (messages[2], messages[0])
			else:
				messages = messages[0]

		yield lineno, funcname, messages, comments

def babel_extract_python(*args, **kwargs):
	"""
	Wrapper around babel's `extract_python`, handling our own implementation of `_()`
	"""
	for lineno, funcname, messages, comments in extract_python(*args, **kwargs):
		if funcname == "_" and isinstance(messages, tuple) and len(messages) > 1:
			funcname = "pgettext"
			messages = (messages[-1], messages[0])  # (context, message)

		yield lineno, funcname, messages, comments


def babel_extract_javascript(fileobj, keywords, comment_tags, options):
	from babel.messages.extract import extract_javascript

	# We use `__` as our translation function
	keywords = "__"

	for lineno, funcname, messages, comments in extract_javascript(
		fileobj, keywords, comment_tags, options
	):
		# `funcname` here will be `__` which is our translation function. We
		# have to convert it back to usual function names
		funcname = "gettext"

		if isinstance(messages, tuple):
			if len(messages) == 3:
				funcname = "pgettext"
				messages = (messages[2], messages[0])
			else:
				messages = messages[0]

		yield lineno, funcname, messages, comments


def babel_extract_doctype_json(fileobj, *args, **kwargs):
	"""
	Extract messages from DocType JSON files. To be used to babel extractor

	:param fileobj: the file-like object the messages should be extracted from
	:rtype: `iterator`
	"""
	data = json.load(fileobj)

	if isinstance(data, list):
		return

	doctype = data.get("name")

	yield None, "_", doctype, ["Name of a DocType"]

	messages = []
	fields = data.get("fields", [])
	links = data.get("links", [])

	for field in fields:
		fieldtype = field.get("fieldtype")

		if label := field.get("label"):
			messages.append((label, f"Label of a {fieldtype} field in DocType '{doctype}'"))

		if description := field.get("description"):
			messages.append((description, f"Description of a {fieldtype} field in DocType '{doctype}'"))

		if message := field.get("options"):
			if fieldtype == "Select":
				select_options = [option for option in message.split("\n") if option and not option.isdigit()]

				if select_options and "icon" in select_options[0]:
					continue

				messages.extend(
					(option, f"Option for a Select field in DocType '{doctype}'") for option in select_options
				)
			elif fieldtype == "HTML":
				messages.append((message, f"Content of an HTML field in DocType '{doctype}'"))

	for link in links:
		if group := link.get("group"):
			messages.append((group, f"Group in {doctype}'s connections"))

		if link_doctype := link.get("link_doctype"):
			messages.append((link_doctype, f"Linked DocType in {doctype}'s connections"))

	# By using "pgettext" as the function name we can supply the doctype as context
	yield from ((None, "pgettext", (doctype, message), [comment]) for message, comment in messages)

	# Role names do not get context because they are used with multiple doctypes
	yield from (
		(None, "_", perm["role"], ["Name of a role"])
		for perm in data.get("permissions", [])
		if "role" in perm
	)


def babel_extract_workspace_json(fileobj, *args, **kwargs):
	"""
	Extract messages from DocType JSON files. To be used to babel extractor

	:param fileobj: the file-like object the messages should be extracted from
	:rtype: `iterator`
	"""
	data = json.load(fileobj)

	if isinstance(data, list):
		return

	if data.get("doctype") != "Workspace":
		return

	workspace_name = data.get("label")

	yield None, "_", workspace_name, ["Name of a Workspace"]
	yield from (
		(None, "_", chart.get("label"), [f"Label of a chart in the {workspace_name} Workspace"])
		for chart in data.get("charts", [])
	)
	yield from (
		(
			None,
			"pgettext",
			(link.get("link_to") if link.get("link_type") == "DocType" else None, link.get("label")),
			[f"Label of a {link.get('type')} in the {workspace_name} Workspace"],
		)
		for link in data.get("links", [])
	)
	yield from (
		(
			None,
			"pgettext",
			(shortcut.get("link_to") if shortcut.get("type") == "DocType" else None, shortcut.get("label")),
			[f"Label of a shortcut in the {workspace_name} Workspace"],
		)
		for shortcut in data.get("shortcuts", [])
	)


def babel_extract_form_tour_json(fileobj, *args, **kwargs):
	"""
	Extract messages from DocType JSON files. To be used to babel extractor

	:param fileobj: the file-like object the messages should be extracted from
	:rtype: `iterator`
	"""
	data = json.load(fileobj)

	if isinstance(data, list):
		return

	if data.get("doctype") != "Form Tour":
		return

	doctype = data.get("name")
	yield None, "_", doctype, ["Name of a DocType"]

	title = data.get("title")
	yield None, "_", title, ["Title of a Form Tour"]

	view_name = data.get("view_name")
	yield None, "_", view_name, ["View Name of a Form Tour"]

	messages = []
	steps = data.get("steps", [])
	for step in steps:
		if title := step.get("title"):
			messages.append((title, f"Title of a step of '{doctype}' Form Tour"))

		if description := step.get("description"):
			messages.append((description, f"Description of a step of '{doctype}' Form Tour"))

		if label := step.get("label"):
			messages.append((label, f"Label of a step of '{doctype}' Form Tour"))

		if ondemand_description := step.get("ondemand_description"):
			messages.append((ondemand_description, f"On Demand Description of a step of '{doctype}' Form Tour"))

	# By using "pgettext" as the function name we can supply the doctype as context
	yield from ((None, "pgettext", (doctype, message), [comment]) for message, comment in messages)

def extract_messages_from_doctype(name):
	"""Extract all translatable messages for a doctype. Includes labels, Python code,
	Javascript code, html templates"""
	messages = []
	meta = frappe.get_meta(name)

	messages = [meta.name, meta.module]

	if meta.description:
		messages.append(meta.description)

	# translations of field labels, description and options
	for d in meta.get("fields"):
		messages.extend([d.label, d.description])

		if d.fieldtype == "Select" and d.options:
			options = d.options.split("\n")
			if not "icon" in options[0]:
				messages.extend(options)
		if d.fieldtype == "HTML" and d.options:
			messages.append(d.options)

	# translations of roles
	for d in meta.get("permissions"):
		if d.role:
			messages.append(d.role)

	messages = [message for message in messages if message]
	messages = [("DocType: " + name, message) for message in messages if is_translatable(message)]

	# workflow based on doctype
	messages.extend(extract_messages_from_workflow(doctype=name))
	return messages

def get_extra_include_js_files(app_name=None):
	"""Returns messages from js files included at time of boot like desk.min.js for desk and web"""
	from frappe.utils.jinja_globals import bundled_asset

	files = []
	app_include_js = frappe.get_hooks("app_include_js", app_name=app_name) or []
	web_include_js = frappe.get_hooks("web_include_js", app_name=app_name) or []
	include_js = app_include_js + web_include_js

	for js_path in include_js:
		file_path = bundled_asset(js_path)
		relative_path = os.path.join(frappe.local.sites_path, file_path.lstrip("/"))

	return [(file, "frappe.translate.babel_extract_javascript") for file in files]

def extract_messages_from_navbar():
	"""Return all labels from Navbar Items, as specified in Navbar Settings."""
	labels = frappe.get_all("Navbar Item", filters={"item_label": ("is", "set")}, pluck="item_label")
	return [("Navbar:", label, "Label of a Navbar Item") for label in labels]

def extract_messages_from_workflow(doctype=None, app_name=None):
	assert doctype or app_name, "doctype or app_name should be provided"

	# translations for Workflows
	workflows = []
	if doctype:
		workflows = frappe.get_all("Workflow", filters={"document_type": doctype})
	else:
		fixtures = frappe.get_hooks("fixtures", app_name=app_name) or []
		for fixture in fixtures:
			if isinstance(fixture, str) and fixture == "Worflow":
				workflows = frappe.get_all("Workflow")
				break
			elif isinstance(fixture, dict) and fixture.get("dt", fixture.get("doctype")) == "Workflow":
				workflows.extend(frappe.get_all("Workflow", filters=fixture.get("filters")))

	messages = []
	document_state = DocType("Workflow Document State")
	for w in workflows:
		states = frappe.db.get_values(
			document_state,
			filters=document_state.parent == w["name"],
			fieldname="state",
			distinct=True,
			as_dict=True,
			order_by=None,
		)
		messages.extend(
			[
				("Workflow: " + w["name"], state["state"])
				for state in states
				if is_translatable(state["state"])
			]
		)
		states = frappe.db.get_values(
			document_state,
			filters=(document_state.parent == w["name"]) & (document_state.message.isnotnull()),
			fieldname="message",
			distinct=True,
			order_by=None,
			as_dict=True,
		)
		messages.extend(
			[
				("Workflow: " + w["name"], state["message"])
				for state in states
				if is_translatable(state["message"])
			]
		)

		actions = frappe.db.get_values(
			"Workflow Transition",
			filters={"parent": w["name"]},
			fieldname="action",
			as_dict=True,
			distinct=True,
			order_by=None,
		)

		messages.extend(
			[
				("Workflow: " + w["name"], action["action"])
				for action in actions
				if is_translatable(action["action"])
			]
		)

	return messages

def extract_messages_from_custom_fields(app_name):
	fixtures = frappe.get_hooks("fixtures", app_name=app_name) or []
	custom_fields = []

	for fixture in fixtures:
		if isinstance(fixture, str) and fixture == "Custom Field":
			custom_fields = frappe.get_all(
				"Custom Field", fields=["name", "label", "description", "fieldtype", "options"]
			)
			break
		elif isinstance(fixture, dict) and fixture.get("dt", fixture.get("doctype")) == "Custom Field":
			custom_fields.extend(
				frappe.get_all(
					"Custom Field",
					filters=fixture.get("filters"),
					fields=["name", "label", "description", "fieldtype", "options"],
				)
			)

	messages = []
	for cf in custom_fields:
		for prop in ("label", "description"):
			if not cf.get(prop) or not is_translatable(cf[prop]):
				continue
			messages.append(("Custom Field - {}: {}".format(prop, cf["name"]), cf[prop]))
		if cf["fieldtype"] == "Selection" and cf.get("options"):
			for option in cf["options"].split("\n"):
				if option and "icon" not in option and is_translatable(option):
					messages.append(("Custom Field - Description: " + cf["name"], option))

	return messages

def extract_messages_from_report(name):
	"""Returns all translatable strings from a :class:`frappe.core.doctype.Report`"""
	report = frappe.get_doc("Report", name)
	messages = []

	if report.columns:
		context = (
			"Column of report '%s'" % report.name
		)  # context has to match context in `prepare_columns` in query_report.js
		messages.extend([(None, report_column.label, context) for report_column in report.columns])

	if report.filters:
		messages.extend([(None, report_filter.label) for report_filter in report.filters])

	if report.query:
		messages.extend(
			[
				(None, message)
				for message in REPORT_TRANSLATE_PATTERN.findall(report.query)
				if is_translatable(message)
			]
		)

	messages.append((None, report.report_name))
	return messages

def extract_messages_from_extras():
	messages = []
	messages += (
		frappe.qb.from_("Print Format").select(PseudoColumn("'Print Format:'"), "name")
	).run()
	messages += (frappe.qb.from_("DocType").select(PseudoColumn("'DocType:'"), "name")).run()
	messages += frappe.qb.from_("Role").select(PseudoColumn("'Role:'"), "name").run()
	messages += (frappe.qb.from_("Module Def").select(PseudoColumn("'Module:'"), "name")).run()
	messages += (
		frappe.qb.from_("Workspace Shortcut")
		.where(Field("format").isnotnull())
		.select(PseudoColumn("''"), "format")
	).run()
	messages += (frappe.qb.from_("Onboarding Step").select(PseudoColumn("''"), "title")).run()

	return messages

@frappe.whitelist()
def get_source_additional_info(source, language=""):
	from frappe.frappeclient import FrappeClient

	translator = FrappeClient(get_translator_url())
	return translator.post_api("translator.api.get_source_additional_info", params=locals())


@frappe.whitelist()
def get_contributions(language):
	return frappe.get_all(
		"Translation",
		fields=["*"],
		filters={
			"contributed": 1,
		},
	)


@frappe.whitelist()
def get_contribution_status(message_id):
	from frappe.frappeclient import FrappeClient

	doc = frappe.get_doc("Translation", message_id)
	translator = FrappeClient(get_translator_url())
	contributed_translation = translator.get_api(
		"translator.api.get_contribution_status",
		params={"translation_id": doc.contribution_docname},
	)
	return contributed_translation


def get_translator_url():
	return frappe.get_hooks()["translator_url"][0]


@frappe.whitelist(allow_guest=True)
def get_all_languages(with_language_name: bool = False) -> list:
	"""Returns all enabled language codes ar, ch etc"""

	def get_language_codes():
		return frappe.get_all("Language", filters={"enabled": 1}, pluck="name")

	def get_all_language_with_name():
		return frappe.get_all("Language", ["language_code", "language_name"], {"enabled": 1})

	if not frappe.db:
		frappe.connect()

	if with_language_name:
		return frappe.cache.get_value("languages_with_name", get_all_language_with_name)
	else:
		return frappe.cache.get_value("languages", get_language_codes)


@frappe.whitelist(allow_guest=True)
def set_preferred_language_cookie(preferred_language: str):
	frappe.local.cookie_manager.set_cookie("preferred_language", preferred_language)


def get_preferred_language_cookie():
	return frappe.request.cookies.get("preferred_language")


def get_translated_doctypes():
	dts = frappe.get_all("DocType", {"translated_doctype": 1}, pluck="name")
	custom_dts = frappe.get_all(
		"Property Setter", {"property": "translated_doctype", "value": "1"}, pluck="doc_type"
	)
	return unique(dts + custom_dts)


@contextmanager
def print_language(language: str):
	"""Ensure correct globals for printing in a specific language.

	Usage:

	```
	with print_language("de"):
	    html = frappe.get_print( ... )
	```
	"""
	if not language or language == frappe.local.lang:
		# do nothing
		yield
		return

	# remember original values
	_lang = frappe.local.lang
	_jenv = frappe.local.jenv

	# set language, empty any existing lang_full_dict and jenv
	frappe.local.lang = language
	frappe.local.jenv = None

	yield

	# restore original values
	frappe.local.lang = _lang
	frappe.local.jenv = _jenv


# Backward compatibility
get_full_dict = get_all_translations
load_lang = get_translations_from_apps
