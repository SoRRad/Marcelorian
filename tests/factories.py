"""Synthetic test data. Every identity here is fake; no real applicant or PI data."""

from __future__ import annotations

import itertools
import random
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from marcelorian.clock import ManualClock
from marcelorian.config.settings import load_settings, set_signature, update_settings
from marcelorian.config.specialty import load_specialty
from marcelorian.db.engine import transaction
from marcelorian.db.models import (
    ApplicantProfile,
    Campaign,
    Institution,
    LabGroup,
    Message,
    Pi,
    PiEmail,
    ProfileFact,
    SendAttempt,
)
from marcelorian.domain.enums import (
    Actor,
    FactProvenance,
    FactStatus,
    MessageState,
    Mode,
)
from marcelorian.domain.gmail_params import REQUIRED_SCOPES
from marcelorian.domain.snapshot import TestOverride
from marcelorian.domain.subjects import expected_subject
from marcelorian.services import approvals, handoff, recipients, runtime
from marcelorian.services.gateway import DispatchResult, SendGateway
from marcelorian.services.sync import SandboxSync
from marcelorian.transport.sandbox import SandboxTransport
from tests.fakes.gmail import FakeGmailTransport
from tests.fakes.sync import FakeRealSync

SPECIALTY = load_specialty().display_name


def _default_body(step: int) -> str:
    return f"Dear Dr. Example,\n\nSynthetic step {step} body.\n\nBest regards,\nA. Applicant"


APPLICANT_EMAIL = "applicant@example.test"
_counter = itertools.count(1)


@dataclass
class World:
    factory: sessionmaker[Session]
    clock: ManualClock
    gmail: FakeGmailTransport = field(init=False)
    sandbox: SandboxTransport = field(init=False)
    real_sync: FakeRealSync = field(init=False)
    gateway: SendGateway = field(init=False)
    applicant_id: int = field(init=False)
    cv_id: int = field(init=False)

    def __post_init__(self) -> None:
        self.gmail = FakeGmailTransport(clock=self.clock, account_email=APPLICANT_EMAIL)
        self.sandbox = SandboxTransport(self.clock)
        self.real_sync = FakeRealSync(self.factory, self.clock)
        self.rebuild_gateway()
        with transaction(self.factory) as s:
            now = self.clock.now()
            applicant = ApplicantProfile(created_at=now, updated_at=now)
            s.add(applicant)
            s.flush()
            self.applicant_id = applicant.id
            runtime.get_runtime(s, now)
            cv = approvals.upload_cv(
                s,
                applicant_id=applicant.id,
                filename="cv-synthetic-v1.pdf",
                content_type="application/pdf",
                data=b"%PDF-1.4 synthetic CV v1",
                actor=Actor.APPLICANT,
                now=now,
            )
            self.cv_id = cv.id
        # The safety tests exercise the strict rules (send window, spacing, 24-hour total,
        # institution limits); tests of the relaxed default turn this off themselves.
        self.settings(strict_send_rules=True)

    def rebuild_gateway(self, seed: int = 7) -> None:
        self.gateway = SendGateway(
            session_factory=self.factory,
            clock=self.clock,
            transports={
                Mode.SANDBOX: self.sandbox,
                Mode.LIVE: self.gmail,
                Mode.TRANSPORT_TEST: self.gmail,
            },
            syncs={"real": self.real_sync, "sandbox": SandboxSync(self.factory, self.clock)},
            specialty_display_name=SPECIALTY,
            rng=random.Random(seed),
        )

    # -- reference data ------------------------------------------------------
    def institution(
        self,
        name: str | None = None,
        tz: str | None = "America/New_York",
        verified: bool = True,
        group_id: int | None = None,
    ) -> int:
        with transaction(self.factory) as s:
            inst = Institution(
                name=name or f"Synthetic University {next(_counter)}",
                city="Testville",
                state="NY",
                iana_tz=tz,
                tz_verified=verified and tz is not None,
                group_id=group_id,
                is_synthetic=True,
                created_at=self.clock.now(),
            )
            s.add(inst)
            s.flush()
            return inst.id

    def pi(
        self,
        institution_id: int,
        *,
        name: str | None = None,
        synthetic: bool = False,
        confidence: str = "A",
        ownership: bool = True,
        salutation_reviewed: bool = True,
        lab_group_id: int | None = None,
        email: str | None = None,
    ) -> int:
        with transaction(self.factory) as s:
            n = next(_counter)
            full = name or f"Pat Example{n}"
            if lab_group_id is None:
                lab = LabGroup(
                    institution_id=institution_id,
                    name=f"{full} lab",
                    is_synthetic=True,
                    created_at=self.clock.now(),
                )
                s.add(lab)
                s.flush()
                lab_group_id = lab.id
            pi = Pi(
                full_name=full,
                last_name=full.split()[-1],
                salutation=f"Dr. {full.split()[-1]}",
                salutation_reviewed=salutation_reviewed,
                institution_id=institution_id,
                lab_group_id=lab_group_id,
                is_synthetic=synthetic,
                created_at=self.clock.now(),
            )
            s.add(pi)
            s.flush()
            s.add(
                PiEmail(
                    pi_id=pi.id,
                    address=email or f"pi{n}@synthetic-univ.test",
                    confidence=confidence,
                    ownership_verified=ownership,
                    source="synthetic",
                    created_at=self.clock.now(),
                )
            )
            return pi.id

    def lab_group_of(self, pi_id: int) -> int:
        with transaction(self.factory) as s:
            pi = s.get(Pi, pi_id)
            assert pi is not None
            return pi.lab_group_id

    def fact(self, key: str, value: str, confirmed: bool = True) -> int:
        with transaction(self.factory) as s:
            fact = ProfileFact(
                applicant_id=self.applicant_id,
                key=key,
                value=value,
                status=(FactStatus.CONFIRMED if confirmed else FactStatus.PROPOSED).value,
                provenance=FactProvenance.APPLICANT_ENTERED.value,
                version=1,
                confirmed_at=self.clock.now() if confirmed else None,
                updated_at=self.clock.now(),
            )
            s.add(fact)
            s.flush()
            return fact.id

    def connect_mailbox(self, email: str = APPLICANT_EMAIL) -> None:
        with transaction(self.factory) as s:
            approvals.connect_mailbox(
                s, email, REQUIRED_SCOPES, actor=Actor.APPLICANT, now=self.clock.now()
            )

    def connect_handoff_mailbox(self, email: str = APPLICANT_EMAIL) -> None:
        with transaction(self.factory) as s:
            approvals.connect_handoff_mailbox(s, email, actor=Actor.APPLICANT, now=self.clock.now())

    def signature_step(self, signature: str = "") -> None:
        with transaction(self.factory) as s:
            set_signature(s, signature, actor=Actor.APPLICANT, now=self.clock.now())

    def hand_off(
        self, message_id: int, *, prior_contact_checked: bool = True, no_reply: bool = True
    ) -> handoff.HandoffResult:
        with transaction(self.factory) as s:
            message = s.get(Message, message_id)
            assert message is not None
            return handoff.hand_off(
                s,
                message,
                handoff.Confirmations(
                    prior_contact_checked=prior_contact_checked, no_reply=no_reply
                ),
                settings=load_settings(s),
                rng=random.Random(11),
                actor=Actor.APPLICANT,
                now=self.clock.now(),
            )

    def handoff_sent(self, attempt_uid: str, *, edited: bool = False) -> None:
        with transaction(self.factory) as s:
            handoff.record_sent(
                s,
                attempt_uid,
                edited=edited,
                settings=load_settings(s),
                actor=Actor.APPLICANT,
                now=self.clock.now(),
            )

    def handoff_not_sent(self, attempt_uid: str, note: str | None = None) -> None:
        with transaction(self.factory) as s:
            handoff.record_not_sent(
                s, attempt_uid, note=note, actor=Actor.APPLICANT, now=self.clock.now()
            )

    def allowlist(self, address: str) -> int:
        with transaction(self.factory) as s:
            return recipients.add_allowlist_address(
                s, address, "test", actor=Actor.APPLICANT, now=self.clock.now()
            ).id

    def set_mode(self, mode: Mode, resume: bool = True) -> None:
        with transaction(self.factory) as s:
            if mode is Mode.LIVE:
                runtime.set_live_readiness(s, True, actor=Actor.APPLICANT, now=self.clock.now())
            runtime.switch_mode(
                s, mode, confirmed=True, actor=Actor.APPLICANT, now=self.clock.now()
            )
            if resume:
                runtime.resume_dispatch(s, actor=Actor.APPLICANT, now=self.clock.now())

    def narrow_window(self) -> None:
        """Narrow the send window to Tue-Thu 08:00-11:00 (the pre-W1 default).

        Tests of window-edge mechanics use it (directly, or via
        ``@pytest.mark.narrow_window`` on the ``world`` fixture) so their edges
        (11:00 end, Thursday close) stay put under the Mon-Fri 08:00-16:00 default.
        """
        self.settings(window_weekdays=(1, 2, 3), window_start="08:00", window_end="11:00")

    def settings(self, **changes: object) -> None:
        with transaction(self.factory) as s:
            update_settings(
                s,
                dict(changes),
                actor=Actor.APPLICANT,
                now=self.clock.now(),
                confirm_less_conservative=True,
            )

    # -- campaigns -------------------------------------------------------------
    def campaign(
        self, pi_id: int, mode: Mode = Mode.SANDBOX, test_recipient_id: int | None = None
    ) -> int:
        with transaction(self.factory) as s:
            return approvals.create_campaign(
                s,
                applicant_id=self.applicant_id,
                pi_id=pi_id,
                mode=mode,
                actor=Actor.APPLICANT,
                now=self.clock.now(),
                test_recipient_id=test_recipient_id,
            ).id

    def draft(
        self,
        campaign_id: int,
        step: int = 1,
        *,
        body: str | None = None,
        cv: bool | None = None,
        facts: tuple[int, ...] = (),
        override: TestOverride | None = None,
        cv_id: int | None = None,
    ) -> int:
        with_cv = cv if cv is not None else step in (1, 4)
        with transaction(self.factory) as s:
            campaign = s.get(Campaign, campaign_id)
            assert campaign is not None
            message = approvals.save_draft(
                s,
                campaign,
                step,
                approvals.DraftContent(
                    subject=expected_subject(step, SPECIALTY),
                    body=body or _default_body(step),
                    cv_version_ids=((cv_id or self.cv_id),) if with_cv else (),
                    fact_ids=facts,
                    test_override=override,
                ),
                actor=Actor.APPLICANT,
                now=self.clock.now(),
            )
            return message.id

    def approve(self, message_id: int, **kwargs: object) -> approvals.ApprovalOutcome:
        with transaction(self.factory) as s:
            message = s.get(Message, message_id)
            assert message is not None
            return approvals.approve(
                s,
                message,
                actor=Actor.APPLICANT,
                now=self.clock.now(),
                settings=load_settings(s),
                specialty_display_name=SPECIALTY,
                **kwargs,  # type: ignore[arg-type]
            )

    def draft_and_approve(self, campaign_id: int, step: int = 1, **kwargs: object) -> int:
        message_id = self.draft(campaign_id, step, **kwargs)  # type: ignore[arg-type]
        self.approve(message_id)
        return message_id

    def dispatch(self, message_id: int) -> DispatchResult:
        return self.gateway.dispatch(message_id)

    def worker_cycle(self, message_id: int) -> DispatchResult:
        """One dispatcher cycle: the worker's regular mailbox sync, then dispatch.

        Spec §9.4 counts an evaluation as functioning only with a current sync, so tests
        that model a healthy worker deferring for caps or pacing use this.
        """
        mode = Mode(self.message(message_id).mode)
        sync = self.real_sync if mode.is_real else SandboxSync(self.factory, self.clock)
        sync.sync_now()
        return self.dispatch(message_id)

    # -- reads -------------------------------------------------------------------
    def message(self, message_id: int) -> Message:
        with transaction(self.factory) as s:
            message = s.get(Message, message_id)
            assert message is not None
            return message

    def campaign_row(self, campaign_id: int) -> Campaign:
        with transaction(self.factory) as s:
            campaign = s.get(Campaign, campaign_id)
            assert campaign is not None
            return campaign

    def message_for(self, campaign_id: int, step: int) -> Message | None:
        with transaction(self.factory) as s:
            return s.scalar(
                select(Message).where(Message.campaign_id == campaign_id, Message.step == step)
            )

    def attempts(self, message_id: int) -> list[SendAttempt]:
        with transaction(self.factory) as s:
            return list(
                s.scalars(
                    select(SendAttempt)
                    .where(SendAttempt.message_id == message_id)
                    .order_by(SendAttempt.id)
                )
            )

    def advance_to(self, when: datetime) -> None:
        self.clock.set(when)

    def message_state(self, message_id: int) -> MessageState:
        return MessageState(self.message(message_id).state)
