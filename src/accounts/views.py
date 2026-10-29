"""Authentication screens.

One step, not two: username, password and TOTP token are submitted together
via django-otp's OTPAuthenticationForm. A two-step flow would need a
half-authenticated session between the steps, which is a state worth not
having.

Enrolment is deliberately absent from the web. A device is created from the
console with `manage.py enrol_totp`, because an enrolment page reachable
before verification is a way to add a second factor using only the first.
"""

import logging

from django.contrib.auth.views import LoginView
from django.http import HttpRequest, HttpResponse
from django.utils.translation import ngettext

from accounts import throttle
from audit.logging import fields

logger = logging.getLogger("steadystack.accounts")


class OperatorLoginView(LoginView):
    template_name = "registration/login.html"
    redirect_authenticated_user = False

    #: Set when an attempt was refused by the throttle rather than by the
    #: credentials. Such an attempt is recorded once, as throttled; counting
    #: it as a failure too would extend the lockout for merely trying again.
    throttled = False

    def get_form_class(self):
        # Imported here rather than at module scope: django-otp's forms pull in
        # the device plugins, which need the app registry to be ready.
        from accounts.forms import OperatorAuthenticationForm

        return OperatorAuthenticationForm

    def post(self, request: HttpRequest, *args, **kwargs) -> HttpResponse:
        username = request.POST.get("username", "")
        state = throttle.status(username, throttle.client_ip(request))

        if not state.locked:
            return super().post(request, *args, **kwargs)

        self.throttled = True
        throttle.note_throttled(username, request, state)
        logger.warning(
            "login throttled",
            extra=fields(subject=throttle.subject_of(username), failures=state.failures),
        )

        minutes = max(1, -(-int(state.retry_after.total_seconds()) // 60))
        form = self.get_form()
        form.add_error(
            None,
            ngettext(
                "Too many failed attempts. Try again in %(minutes)d minute.",
                "Too many failed attempts. Try again in %(minutes)d minutes.",
                minutes,
            )
            % {"minutes": minutes},
        )
        return self.form_invalid(form)

    def form_valid(self, form):
        username = form.cleaned_data.get("username", "")
        throttle.note_success(username, self.request)
        logger.info("login succeeded", extra=fields(subject=throttle.subject_of(username)))
        return super().form_valid(form)

    #: Failures left before the lockout, after a failed attempt. Counted per
    #: subject and address whether or not the account exists, so it tells a
    #: stranger nothing the lockout itself would not.
    attempts_left: int | None = None

    def form_invalid(self, form):
        if not self.throttled:
            username = form.data.get("username", "")
            throttle.note_failure(username, self.request)
            logger.warning("login failed", extra=fields(subject=throttle.subject_of(username)))
            state = throttle.status(username, throttle.client_ip(self.request))
            if not state.locked:
                self.attempts_left = max(0, throttle.MAX_FAILURES - state.failures)
        return super().form_invalid(form)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["attempts_left"] = self.attempts_left
        context["max_failures"] = throttle.MAX_FAILURES
        context["lockout_minutes"] = int(throttle.WINDOW.total_seconds() // 60)
        return context
