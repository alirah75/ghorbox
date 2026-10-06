"""Actor-bound expiring tokens; callback payload never contains identity."""

import secrets
import time

from app.core.permissions import InputError


class Navigation:
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.tokens = {}
        self.forms = {}

    def prune(self):
        self.tokens = {k: v for k, v in self.tokens.items() if v[0] > self.clock()}
        self.forms = {k: v for k, v in self.forms.items() if v[0] > self.clock()}

    def issue(self, actor, action, payload=None):
        self.prune()
        if len(self.tokens) >= 5000:
            raise InputError("سامانه مشغول است؛ کمی بعد تلاش کنید.")
        token = "a:" + secrets.token_urlsafe(18)
        self.tokens[token] = (self.clock() + 600, actor, action, payload)
        return token

    def consume(self, actor, token):
        self.prune()
        value = self.tokens.get(token)
        if value is None or value[1] != actor:
            raise InputError("دکمه نامعتبر یا منقضی است؛ از /start استفاده کنید.")
        del self.tokens[token]
        return value[2], value[3]

    def set_form(self, actor, action, payload=None):
        self.prune()
        if actor not in self.forms and len(self.forms) >= 1000:
            raise InputError("سامانه موقتاً مشغول است.")
        self.forms[actor] = (self.clock() + 600, action, payload)

    def get_form(self, actor):
        self.prune()
        value = self.forms.get(actor)
        return (value[1], value[2]) if value else None

    def cancel(self, actor):
        self.forms.pop(actor, None)
        self.tokens = {k: v for k, v in self.tokens.items() if v[1] != actor}
