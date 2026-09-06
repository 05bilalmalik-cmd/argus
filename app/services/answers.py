from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AnswerEntry
from app.repositories import AnswerRepository
from app.security.audit import AuditInput, append_audit
from app.security.crypto import CryptoBox

_STOPWORDS = {
    "the", "and", "for", "are", "you", "your", "have", "has", "did", "does",
    "this", "that", "with", "please", "select", "indicate", "provide",
    "whether", "what", "which", "will", "can", "may", "any", "all", "not",
}


@dataclass(frozen=True, slots=True)
class ResolvedAnswer:
    value: str
    source: str
    canonical_key: str
    sensitive: bool


class AnswerService:
    def __init__(self, session: Session, crypto: CryptoBox):
        self.session = session
        self.crypto = crypto
        self.repository = AnswerRepository(session)

    def upsert(
        self,
        *,
        canonical_key: str,
        prompt: str,
        answer: str,
        approved: bool,
        sensitive: bool,
        category: str = "general",
        evidence: str = "",
        max_characters: int | None = None,
        actor: str = "user",
    ) -> AnswerEntry:
        entry = self.repository.by_key(canonical_key)
        created = entry is None
        if entry is None:
            entry = AnswerEntry(canonical_key=canonical_key)
            self.session.add(entry)
        entry.prompt = prompt
        entry.answer_ciphertext = self.crypto.encrypt(answer)
        entry.approved = approved
        entry.sensitive = sensitive
        entry.category = category
        entry.evidence = evidence
        entry.max_characters = max_characters
        self.session.flush()
        append_audit(
            self.session,
            AuditInput(
                actor,
                "answer.created" if created else "answer.updated",
                "answer",
                entry.id,
                {
                    "canonical_key": canonical_key,
                    "approved": approved,
                    "sensitive": sensitive,
                },
            ),
        )
        return entry

    def resolve(self, canonical_key: str, label: str = "") -> ResolvedAnswer | None:
        entry = self.repository.by_key(canonical_key)
        if entry is None or not entry.approved or not entry.answer_ciphertext:
            return self._fuzzy_resolve(label)
        return ResolvedAnswer(
            value=self.crypto.decrypt(entry.answer_ciphertext),
            source="answer_bank",
            canonical_key=entry.canonical_key,
            sensitive=entry.sensitive,
        )

    def _fuzzy_resolve(self, label: str) -> ResolvedAnswer | None:
        """Custom form questions rarely match canonical keys verbatim. Match
        the form label against stored prompts ("penultimate year", "eligible
        to work in the UK") and reuse that answer when confidently similar."""
        if not label or len(label) < 8:
            return None

        def tokens(text: str) -> set[str]:
            return {
                token
                for token in re.findall(r"[a-z][a-z0-9']+", text.casefold())
                if token not in _STOPWORDS and len(token) > 2
            }

        wanted = tokens(label)
        if len(wanted) < 2:
            return None

        best: tuple[float, AnswerEntry] | None = None
        for entry in self.session.scalars(
            select(AnswerEntry).where(AnswerEntry.approved.is_(True))
        ).all():
            if not entry.answer_ciphertext or entry.sensitive:
                continue
            prompt_tokens = tokens(entry.prompt or "") | tokens(
                entry.canonical_key.replace("_", " ")
            )
            if not prompt_tokens:
                continue
            overlap = len(wanted & prompt_tokens) / len(wanted)
            if best is None or overlap > best[0]:
                best = (overlap, entry)
        if best is None or best[0] < 0.5:
            return None
        overlap, entry = best
        return ResolvedAnswer(
            value=self.crypto.decrypt(entry.answer_ciphertext),
            source=f"answer_bank:fuzzy({overlap:.0%})",
            canonical_key=entry.canonical_key,
            sensitive=entry.sensitive,
        )

    def list_public(self) -> list[dict[str, object]]:
        return [
            {
                "id": entry.id,
                "canonical_key": entry.canonical_key,
                "prompt": entry.prompt,
                "category": entry.category,
                "sensitive": entry.sensitive,
                "approved": entry.approved,
                "evidence": entry.evidence,
                "max_characters": entry.max_characters,
                "answer_preview": (
                    self.crypto.decrypt(entry.answer_ciphertext)[:120]
                    if entry.answer_ciphertext
                    else ""
                ),
            }
            for entry in self.repository.list()
        ]
