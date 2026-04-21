# Returns and refunds

Amounts are in US dollars. "Order total" means item prices plus the shipping fee.
"Refundable base" means the order total minus the price of any final-sale item (see R5).

## R1 — Return window
Returns and refund requests are accepted within 30 days of delivery. The delivery date is the
timestamp of the carrier `DELIVERED` event. A request made more than 30 days after delivery is
denied, whatever the reason.

## R2 — Damaged on arrival
If an order arrived damaged, refund the full order total, including shipping and including any
final-sale items, provided the carrier recorded a `DAMAGED_SCAN` event or the customer supplied a
photo of the damage. With neither, the claim is denied. The request must still fall inside the
R1 window.

## R3 — Not delivered
If the order has no `DELIVERED` event, refund the refundable base once either a `LOST` event exists
or at least 7 days have passed since the promised delivery date. Before that, deny the request and
tell the customer to wait. If the customer says the order did not arrive but tracking shows a
`DELIVERED` event, tracking decides: handle the case under R4, not R3.

## R4 — Marked delivered but customer says it is missing
If tracking shows `DELIVERED` and the customer says the parcel never arrived: auto-approve a refund
of the refundable base when the order total is under $50 and the customer has not had an R4
approval before. In every other case (order total of $50 or more, or a previous R4 approval)
escalate to a human. The request must still fall inside the R1 window.

## R5 — Final-sale items
Final-sale items are never refundable, except under R2. When a refund is calculated under R3, R4 or
R10 the price of every final-sale item is left out of the amount.

## R6 — Late delivery
If the `DELIVERED` event is later than the promised delivery date, refund the shipping fee only.
This is a partial refund. If the shipping fee was zero, deny.

## R10 — Change of mind
Within the R1 window a customer may return an order because they changed their mind. Refund the sum
of the non-final-sale item prices. The shipping fee is not refunded. After the window, deny.
