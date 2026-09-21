"""Structural HTML context extractor for non-ATS career-portal fields.

Custom portals (Rothschild, Point72, Jane Street, Moelis, Carlyle, Perella
Weinberg, Wells Fargo, PSP, Neuberger Berman, ...) use nonstandard ``name``
attributes, so :class:`DeterministicClassifier
<app.automation.classifier.DeterministicClassifier>` — which matches mostly
on a question's own label/name/placeholder text — maps them to UNKNOWN.

This module supplies the missing structural context: the associated
``<label>``, fieldset legend, section heading, help text, radio-group
options, required markers, input type and select options.  It is
deterministic extraction only (beautifulsoup4, no ML, no new dependencies)
and never raises on bad input.

Bridge to the existing model: :meth:`FieldContext.to_form_question`
returns an :class:`FormQuestion <app.domain.questions.FormQuestion>` whose
``label`` is enriched with group/legend/heading context so the existing
classifier can match against it.  A separate agent consumes this context.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

from bs4 import BeautifulSoup, Tag

from app.domain.questions import FormQuestion

__all__ = [
    "FieldContext",
    "extract_field_context",
    "field_context_to_question",
]

_HEADING_TAGS = frozenset({"h1", "h2", "h3", "h4", "h5", "h6"})

# Sibling/class hints that mark help text (also matched case-insensitively
# against class/id substrings).
_HINT_CLASS_RE = re.compile(
    r"hint|help|desc|note|subtext|sub-label|sublabel|guidance|assist|error|feedback",
    re.IGNORECASE,
)

_REQUIRED_STAR_RE = re.compile(r"\*")

_WHITESPACE_RE = re.compile(r"\s+")


def _clean(text: object) -> str:
    """Normalise whitespace; never raise."""
    try:
        if text is None:
            return ""
        return _WHITESPACE_RE.sub(" ", str(text)).strip()
    except Exception:
        return ""


def _text_of(node: object) -> str:
    try:
        if node is None:
            return ""
        if isinstance(node, Tag):
            return _clean(node.get_text(" "))
        return _clean(node)
    except Exception:
        return ""


def _label_text_without_controls(label: Tag) -> str:
    """Wrapped-label text minus nested inputs/selects/textareas (JS parity)."""
    try:
        clone = BeautifulSoup(str(label), "html.parser")
        for control in clone.find_all(["input", "select", "textarea", "button"]):
            control.decompose()
        return _clean(clone.get_text(" "))
    except Exception:
        return _text_of(label)


def _parse(html: object) -> BeautifulSoup | None:
    try:
        if html is None:
            return None
        text = html if isinstance(html, str) else str(html)
        if not text or not text.strip():
            return None
        return BeautifulSoup(text, "html.parser")
    except Exception:
        return None


def _first_field_element(soup: BeautifulSoup) -> Tag | None:
    try:
        found = soup.find(["input", "select", "textarea", "button"])
        return found if isinstance(found, Tag) else None
    except Exception:
        return None


def _resolve_ids(soup: BeautifulSoup, ids: str) -> str:
    parts: list[str] = []
    try:
        for token in ids.split():
            token = token.strip().strip(",")
            if not token:
                continue
            try:
                node = soup.find(id=token)
            except Exception:
                node = None
            text = _text_of(node)
            if text:
                parts.append(text)
    except Exception:
        pass
    return _clean(" ".join(parts))


def _explicit_label(soup: BeautifulSoup, element_id: str) -> str:
    if not element_id:
        return ""
    try:
        label = soup.find("label", attrs={"for": element_id})
        if isinstance(label, Tag):
            return _label_text_without_controls(label)
    except Exception:
        pass
    return ""


def _wrapping_label(element: Tag) -> str:
    try:
        parent = element
        while parent is not None:
            parent = getattr(parent, "parent", None)
            if isinstance(parent, Tag) and parent.name == "label":
                return _label_text_without_controls(parent)
            if parent is None or getattr(parent, "name", None) in ("form", "body", "html"):
                break
    except Exception:
        pass
    return ""


def _preceding_sibling_label(element: Tag) -> str:
    """Find nearest preceding sibling <label> that doesn't contain other inputs."""
    try:
        for sibling in getattr(element, "previous_siblings", []) or []:
            if not isinstance(sibling, Tag):
                continue
            if sibling.name == "label":
                # Skip labels that wrap other inputs (belongs to another field)
                if sibling.find(["input", "select", "textarea"]):
                    continue
                return _label_text_without_controls(sibling)
            # Stop at block-level elements or other fields
            if sibling.name in ("input", "select", "textarea", "button", "fieldset", "h1", "h2", "h3", "h4", "h5", "h6", "div", "p", "section", "article"):
                if sibling.find(["input", "select", "textarea"]):
                    break
    except Exception:
        pass
    return ""


def _field_container(element: Tag) -> Tag:
    """Find the nearest ancestor that contains this field and no other unrelated fields.

    Returns the element itself if no suitable container found (e.g., direct child of form).
    """
    try:
        current = element
        while True:
            parent = getattr(current, "parent", None)
            if parent is None or parent.name in ("form", "body", "html", "fieldset"):
                return current
            # Check if parent contains other inputs besides our element
            inputs = parent.find_all(["input", "select", "textarea"])
            other_inputs = []
            for inp in inputs:
                if inp is element:
                    continue
                # If it's a radio/checkbox with same name, it's part of our group
                if (inp.get("name") == element.get("name")
                    and inp.get("type", "").casefold() in ("radio", "checkbox")):
                    continue
                other_inputs.append(inp)
            if other_inputs:
                return current
            current = parent
    except Exception:
        return element


def _fieldset_legend(element: Tag) -> str:
    try:
        node: object = element
        while node is not None:
            node = getattr(node, "parent", None)
            if isinstance(node, Tag) and node.name == "fieldset":
                try:
                    legend = node.find("legend", recursive=False)
                except Exception:
                    legend = None
                text = _text_of(legend)
                if text:
                    return text
                return ""
    except Exception:
        pass
    return ""


def _describedby_text(soup: BeautifulSoup, element: Tag) -> str:
    try:
        ref = element.get("aria-describedby") or ""
        if isinstance(ref, (list, tuple)):
            ref = " ".join(str(v) for v in ref)
        text = _resolve_ids(soup, str(ref or ""))
        if text:
            return text
    except Exception:
        pass
    return ""


def _adjacent_hint_text(element: Tag) -> str:
    """Hint text from siblings / container hints (class-guided, conservative)."""
    hints: list[str] = []
    try:
        element_id = element.get("id", "") or ""
        # Check next siblings (up to 4)
        for sibling in list(getattr(element, "next_siblings", []) or [])[:4]:
            if not isinstance(sibling, Tag):
                continue
            if sibling.name in ("input", "select", "textarea", "button", "fieldset"):
                # A neighbouring control's own block is not our hint; stop traversal.
                break
            if sibling.name == "label":
                # Stop if this label is for a different field
                if sibling.get("for") and sibling.get("for") != element_id:
                    break
                continue
            blob = _clean(" ".join(str(v) for v in (sibling.get("class", []), sibling.get("id", ""))))
            if sibling.name in ("small", "span", "div", "p", "em") and (
                _HINT_CLASS_RE.search(blob) or sibling.name == "small"
            ):
                text = _text_of(sibling)
                if text and len(text) <= 500:
                    hints.append(text)
        # Check previous siblings (up to 4) - hints can appear before the input
        for sibling in list(getattr(element, "previous_siblings", []) or [])[:4]:
            if not isinstance(sibling, Tag):
                continue
            if sibling.name in ("input", "select", "textarea", "button", "fieldset"):
                # A neighbouring control's own block is not our hint; stop traversal.
                break
            if sibling.name == "label":
                # Stop if this label is for a different field
                if sibling.get("for") and sibling.get("for") != element_id:
                    break
                # A previous control can own the hint before our label.
                # Without such a control, keep the field's genuine leading help.
                if sibling.get("for") == element_id and any(
                    isinstance(previous, Tag) and (
                        previous.name in ("input", "select", "textarea", "button", "fieldset")
                        or previous.find(["input", "select", "textarea", "button"])
                    )
                    for previous in sibling.previous_siblings
                ):
                    break
                continue
            blob = _clean(" ".join(str(v) for v in (sibling.get("class", []), sibling.get("id", ""))))
            if sibling.name in ("small", "span", "div", "p", "em") and (
                _HINT_CLASS_RE.search(blob) or sibling.name == "small"
            ):
                text = _text_of(sibling)
                if text and len(text) <= 500 and text not in hints:
                    hints.append(text)
        # Search within the field's container (not the entire parent tree)
        container = _field_container(element)
        if container is not element:
            for candidate in container.find_all(["small", "span", "div", "p"], limit=12):
                if candidate is element:
                    continue
                if isinstance(candidate, Tag) and candidate.find(["input", "select", "textarea"]):
                    continue
                blob = _clean(
                    " ".join(str(v) for v in (candidate.get("class", []), candidate.get("id", "")))
                    + " "
                    + str(candidate.get("role", "") or "")
                )
                if _HINT_CLASS_RE.search(blob):
                    text = _text_of(candidate)
                    if text and len(text) <= 500 and text not in hints:
                        hints.append(text)
                        break
    except Exception:
        pass
    return _clean(" ".join(hints))


def _nearest_heading(soup: BeautifulSoup, element: Tag) -> str:
    """Nearest preceding h1-h6 / role=heading in document order."""
    try:
        ordered = soup.find_all(True)
        index = -1
        for i, node in enumerate(ordered):
            if node is element:
                index = i
                break
        if index < 0:
            return ""
        for node in reversed(ordered[:index]):
            if not isinstance(node, Tag):
                continue
            name = (node.name or "").casefold()
            if name in _HEADING_TAGS:
                text = _text_of(node)
                if text:
                    return text
            else:
                try:
                    if _clean(node.get("role", "") or "").casefold() == "heading":
                        text = _text_of(node)
                        if text:
                            return text
                except Exception:
                    continue
    except Exception:
        pass
    return ""


def _option_label_for(soup: BeautifulSoup, option: Tag) -> str:
    try:
        opt_id = _clean(option.get("id", "") or "")
        if opt_id:
            text = _explicit_label(soup, opt_id)
            if text:
                return text
        text = _wrapping_label(option)
        if text:
            return text
        for attr in ("aria-label", "data-label"):
            text = _clean(option.get(attr, "") or "")
            if text:
                return text
        labelledby = option.get("aria-labelledby") or ""
        if isinstance(labelledby, (list, tuple)):
            labelledby = " ".join(str(v) for v in labelledby)
        text = _resolve_ids(soup, str(labelledby or ""))
        if text:
            return text
        text = _clean(option.get("value", "") or "")
        if text and not re.fullmatch(r"on|off|true|false|1|0", text, re.IGNORECASE):
            return text
        return text
    except Exception:
        return ""


def _find_twin(doc_soup: BeautifulSoup, snippet_element: Tag) -> Tag | None:
    """Locate the snippet element's twin inside the document.

    Id match first; fallback to tag/type/name when the portal omits ids
    (common on custom portals).  Returns None when ambiguous or absent.
    """
    try:
        snippet_id = _clean(snippet_element.get("id", "") or "")
        if snippet_id:
            twin = doc_soup.find(id=snippet_id)
            if isinstance(twin, Tag) and twin.name in (
                "input",
                "select",
                "textarea",
                "button",
            ):
                return twin
            return None
        tag = (snippet_element.name or "").casefold()
        snippet_name = _clean(snippet_element.get("name", "") or "")
        if not snippet_name:
            return None
        snippet_type = _normalise_input_type(snippet_element)
        candidates: list[Tag] = []
        for node in doc_soup.find_all(tag or True):
            if not isinstance(node, Tag):
                continue
            if tag and (node.name or "").casefold() != tag:
                continue
            if _clean(node.get("name", "") or "") != snippet_name:
                continue
            if tag == "input" and _normalise_input_type(node) != snippet_type:
                continue
            candidates.append(node)
        if len(candidates) == 1:
            return candidates[0]
        # Radio/checkbox groups share one name by design; the first member's
        # position yields the shared legend/heading, and group options cover
        # every member, so aligning to it is still useful context.
        if candidates and snippet_type in ("radio", "checkbox"):
            return candidates[0]
        return None
    except Exception:
        return None


def _group_members(soup: BeautifulSoup, element: Tag, input_type: str) -> list[Tag]:
    try:
        name = _clean(element.get("name", "") or "")
        if not name or input_type not in ("radio", "checkbox"):
            return [element]
        members: list[Tag] = []
        for node in soup.find_all("input"):
            if not isinstance(node, Tag):
                continue
            if (node.get("type", "text") or "text").casefold() != input_type:
                continue
            if _clean(node.get("name", "") or "") == name:
                members.append(node)
        return members or [element]
    except Exception:
        return [element]


def _group_label(
    soup: BeautifulSoup, element: Tag, members: Iterable[Tag], legend: str, heading: str
) -> str:
    members = list(members)
    # 1. Shared fieldset legend (JS parity with generic adapter for radios).
    if legend:
        return legend
    # 2. A <legend>-less group wrapper with an explicit question element,
    #    e.g. <div class="question"><span>Are you ...?</span><input .../>.
    try:
        parent = getattr(element, "parent", None)
        depth = 0
        while isinstance(parent, Tag) and depth < 4:
            if parent.name in ("fieldset", "form", "body", "html"):
                break
            owns_all = all(
                m is parent or (isinstance(m, Tag) and parent in list(m.parents))
                for m in members
            ) if members else False
            if owns_all:
                for child in parent.find_all(["span", "div", "p", "label", "legend"], limit=8):
                    if not isinstance(child, Tag):
                        continue
                    if child.find(["input", "select", "textarea"]):
                        continue
                    if child.find_parent("label") is not None:
                        continue
                    text = _text_of(child)
                    if text and len(text) <= 300:
                        return text
                break
            parent = getattr(parent, "parent", None)
            depth += 1
    except Exception:
        pass
    # 3. Nearest preceding heading / section title.
    if heading:
        return heading
    return ""


def _normalise_input_type(element: Tag | None) -> str:
    try:
        if element is None or not isinstance(element, Tag):
            return ""
        tag = (element.name or "").casefold()
        if tag == "select":
            return "select"
        if tag == "textarea":
            return "textarea"
        if tag == "button":
            return _clean(element.get("type", "") or "") or "button"
        if tag == "input":
            return (_clean(element.get("type", "") or "") or "text").casefold()
        return tag or ""
    except Exception:
        return ""


def _select_options(element: Tag) -> tuple[tuple[str, ...], tuple[str, ...]]:
    texts: list[str] = []
    values: list[str] = []
    try:
        if (element.name or "").casefold() != "select":
            return (), ()
        for option in element.find_all("option"):
            if not isinstance(option, Tag):
                continue
            text = _text_of(option)
            value = _clean(option.get("value", "") or "")
            if text:
                texts.append(text)
            elif value:
                texts.append(value)
            values.append(value)
    except Exception:
        pass
    return tuple(texts), tuple(values)


def _dedupe(parts: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for part in parts:
        text = _clean(part)
        if not text:
            continue
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(text)
    return out


@dataclass(frozen=True, slots=True)
class FieldContext:
    """Structural context for one form field.  Partial on bad input."""

    label: str = ""
    placeholder: str = ""
    title: str = ""
    name: str = ""
    element_id: str = ""
    autocomplete: str = ""
    fieldset_legend: str = ""
    help_text: str = ""
    section_heading: str = ""
    group_label: str = ""
    group_options: tuple[str, ...] = ()
    required: bool = False
    required_evidence: tuple[str, ...] = ()
    input_type: str = ""
    select_options: tuple[str, ...] = ()
    option_values: tuple[str, ...] = ()
    context_text: str = ""
    extra_label_bits: tuple[str, ...] = ()

    def to_form_question(self) -> FormQuestion:
        """Bridge to the existing :class:`FormQuestion` shape.

        ``label`` carries the structural signals (label, group question,
        legend, heading, help, aria-labelledby, aria-describedby) so
        :class:`DeterministicClassifier` can match against it; ``options`` carries
        radio/checkbox group options or select option texts.
        """
        # Build label parts including aria-labelledby/describedby context
        label_parts = _dedupe(
            [
                self.label,
                *self.extra_label_bits,
                self.group_label,
                self.fieldset_legend,
                self.section_heading,
                self.help_text,  # includes describedby + adjacent hints
            ]
        )
        options = self.group_options or self.select_options
        return FormQuestion(
            label=_clean(" — ".join(label_parts)),
            field_type=self.input_type or "text",
            name=self.name,
            placeholder=_clean(" ".join(_dedupe([self.placeholder, self.title]))),
            required=self.required,
            options=tuple(options),
        )


def field_context_to_question(context: FieldContext) -> FormQuestion:
    """Functional alias of :meth:`FieldContext.to_form_question`."""
    return context.to_form_question()


def extract_field_context(
    field_html: str | None = None,
    document_html: str | None = None,
    *,
    selector: str | None = None,
) -> FieldContext:
    """Extract structural context for one form field.  Never raises.

    Args:
        field_html: HTML snippet of the field (``<input>``/``<select>``/
            ``<textarea>``), optionally including wrapping label/fieldset.
        document_html: surrounding full-document HTML used for ``for=``/``id``
            labels, ``aria-labelledby``/``aria-describedby`` targets,
            fieldset legends, headings, and radio/checkbox group members.
        selector: optional CSS selector locating the field inside
            ``document_html``.  Takes precedence over snippet matching.

    Returns:
        A :class:`FieldContext`; empty/partial when input is malformed or
        the field cannot be located.
    """
    try:
        if (field_html is None or not _clean(field_html)) and (
            document_html is None or not _clean(document_html)
        ) and not selector:
            return FieldContext()

        doc_soup = _parse(document_html)
        snippet_soup = _parse(field_html)

        target: Tag | None = None
        scope: BeautifulSoup | None = None  # soup used for id lookups + order

        # 1. Explicit selector inside the document (most precise position).
        if selector and doc_soup is not None:
            try:
                found = doc_soup.select_one(selector)
                if isinstance(found, Tag):
                    target = found
                    scope = doc_soup
            except Exception:
                target = None

        snippet_element: Tag | None = None
        if snippet_soup is not None:
            snippet_element = _first_field_element(snippet_soup)
            if snippet_element is None:
                # Snippet may be a bare label/fieldset fragment; keep scope
                # so callers still get a partial context instead of nothing.
                snippet_element = None

        # 2. No selector hit: align the snippet element with its document
        #    twin (by id, else tag/type/name) so wrapping labels, legends,
        #    headings and groups resolve by document position.
        if target is None and doc_soup is not None and snippet_element is not None:
            twin = _find_twin(doc_soup, snippet_element)
            if twin is not None:
                target = twin
                scope = doc_soup
        if target is None and snippet_element is not None:
            target = snippet_element
            # Prefer the document for cross-references, fall back to snippet.
            scope = doc_soup if doc_soup is not None else snippet_soup
        if target is None and doc_soup is not None and selector is None:
            # document-only call: first field in the document.
            target = _first_field_element(doc_soup)
            scope = doc_soup
        if target is None or scope is None:
            return FieldContext()

        # Direct attributes off the target element.
        placeholder = _clean(target.get("placeholder", "") or "")
        title = _clean(target.get("title", "") or "")
        name = _clean(target.get("name", "") or "")
        element_id = _clean(target.get("id", "") or "")
        autocomplete = _clean(target.get("autocomplete", "") or "")
        input_type = _normalise_input_type(target)

        # Associated label: explicit for=/id, wrapping label, preceding sibling label,
        # aria-label, aria-labelledby (mirrors generic adapter's labelFor order, minus
        # the radio-legend special case handled via fieldset_legend below).
        label = ""
        if element_id:
            label = _explicit_label(scope, element_id)
        if not label:
            label = _wrapping_label(target)
        if not label:
            label = _preceding_sibling_label(target)
        aria_label = ""
        try:
            aria_label = _clean(target.get("aria-label", "") or "")
        except Exception:
            aria_label = ""
        labelledby_text = ""
        try:
            labelledby = target.get("aria-labelledby") or ""
            if isinstance(labelledby, (list, tuple)):
                labelledby = " ".join(str(v) for v in labelledby)
            labelledby_text = _resolve_ids(scope, str(labelledby or ""))
        except Exception:
            labelledby_text = ""
        if not label:
            label = aria_label or labelledby_text
        # Keep aria variants for context_text even when a <label> won.
        extra_label_bits = _dedupe(
            [b for b in (aria_label, labelledby_text) if b and b.casefold() != label.casefold()]
        )

        legend = _fieldset_legend(target)
        describedby = _describedby_text(scope, target)
        adjacent_hint = _adjacent_hint_text(target)
        help_text = _clean(" ".join(_dedupe([describedby, adjacent_hint])))
        try:
            in_scope = any(node is target for node in scope.find_all(True))
        except Exception:
            in_scope = False
        heading = _nearest_heading(scope, target) if in_scope else ""

        members = _group_members(scope, target, input_type)
        if len(members) > 1:
            group_options = tuple(
                _dedupe([_option_label_for(scope, m) for m in members])
            )
        else:
            group_options = ()
        group_label = ""
        if len(members) > 1 or input_type in ("radio", "checkbox"):
            group_label = _group_label(scope, target, members, legend, heading)

        # Required: required attr, aria-required="true", "*" in label text.
        evidence: list[str] = []
        try:
            if target.has_attr("required"):
                evidence.append("required-attr")
        except Exception:
            pass
        try:
            if _clean(target.get("aria-required", "") or "").casefold() in ("true", "1"):
                evidence.append("aria-required")
        except Exception:
            pass
        try:
            if _REQUIRED_STAR_RE.search(label or ""):
                evidence.append("asterisk-in-label")
        except Exception:
            pass
        required = bool(evidence)

        select_options, option_values = _select_options(target)

        # context_text in priority order for a classifier to match against:
        # own label first, then group question, legend, section heading,
        # placeholder/title/help, option labels, and finally raw name/id/
        # autocomplete tokens (nonstandard on custom portals, kept last).
        context_parts: list[str] = [
            label,
            *extra_label_bits,
            group_label if group_label.casefold() != label.casefold() else "",
            legend if legend.casefold() != (group_label or "").casefold() else "",
            heading,
            placeholder,
            title,
            help_text,
            " ".join(group_options) if group_options else "",
            " ".join(select_options) if select_options else "",
            name,
            element_id,
            autocomplete,
        ]
        context_text = _clean(" ".join(_dedupe(context_parts)))

        return FieldContext(
            label=label,
            placeholder=placeholder,
            title=title,
            name=name,
            element_id=element_id,
            autocomplete=autocomplete,
            fieldset_legend=legend,
            help_text=help_text,
            section_heading=heading,
            group_label=group_label,
            group_options=group_options,
            required=required,
            required_evidence=tuple(evidence),
            input_type=input_type,
            select_options=select_options,
            option_values=option_values,
            context_text=context_text,
            extra_label_bits=tuple(extra_label_bits),
        )
    except Exception:
        try:
            return FieldContext()
        except Exception:  # pragma: no cover - dataclass construction cannot fail
            raise
