"""The postback endpoint.

Public by necessity: postbacks come from Kite's servers, not from the
operator's browser, so no session exists and no IP restriction applies to
them. The checksum is therefore the only authentication this endpoint has,
and an unverified post is rejected and logged rather than examined.

It answers 200 to anything it accepted responsibility for, including a
postback it could not match to an order -- that one is queued. Kite retries a
non-200, and retrying will not make an order exist that does not.
"""

import json
import logging

from django.http import HttpRequest, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from audit.events import record
from audit.logging import fields
from broker import get_broker
from orders.postback import InvalidPostback, handle

logger = logging.getLogger("steadystack.orders")


@csrf_exempt
@require_POST
def kite_postback(request: HttpRequest) -> JsonResponse:
    try:
        payload = json.loads(request.body or b"{}")
    except ValueError:
        logger.warning("postback body was not json")
        return JsonResponse({"status": "rejected"}, status=400)

    if not isinstance(payload, dict):
        return JsonResponse({"status": "rejected"}, status=400)

    try:
        outcome = handle(payload, broker=get_broker())
    except InvalidPostback as invalid:
        # Anything can find this URL. A post that cannot prove where it came
        # from is recorded and refused, never acted on.
        record(
            "postback.rejected",
            entity_type="orders.Order",
            after={"reason": str(invalid), "order_id": payload.get("order_id")},
        )
        logger.warning("postback rejected", extra=fields(reason=str(invalid)))
        return JsonResponse({"status": "rejected"}, status=403)

    return JsonResponse({"status": "queued" if outcome.queued else "ok"})
