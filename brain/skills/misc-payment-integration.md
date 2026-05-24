---
name: payment-integration
description: Use when integrating payment gateways like Stripe, PayPal, or implementing PCI compliance. Covers checkout flows, webhooks, subscriptions, and security best practices.
summary: Payment integration with Stripe, PayPal, subscription billing, webhook handling, and PCI-DSS compliance patterns.
triggers: [Stripe, PayPal, payment gateway, checkout, subscription, webhook, PCI, billing]
disable-model-invocation: true

---
# Payment Integration (Unified)

## Goal
Integrate payment processing securely with proper error handling, webhook processing, and PCI compliance.

## When to Use
- Integrating Stripe or PayPal
- Building checkout flows
- Implementing subscriptions
- Processing webhooks
- Ensuring PCI compliance
- Handling refunds and disputes

## Stripe Integration

### Installation & Setup
```bash
pip install stripe  # Python
npm install stripe  # Node.js
```

```python
import stripe
stripe.api_key = os.environ["STRIPE_SECRET_KEY"]
```

### One-Time Payment
```python
# Create PaymentIntent (server-side)
def create_payment_intent(amount: int, currency: str = "usd"):
    """Create payment intent for one-time payment."""
    return stripe.PaymentIntent.create(
        amount=amount,  # Amount in cents
        currency=currency,
        automatic_payment_methods={"enabled": True},
        metadata={"order_id": "ord_123"},
    )

# API endpoint
@app.post("/api/create-payment-intent")
async def create_payment(request: PaymentRequest):
    try:
        intent = create_payment_intent(
            amount=request.amount,
            currency=request.currency
        )
        return {"clientSecret": intent.client_secret}
    except stripe.error.StripeError as e:
        raise HTTPException(status_code=400, detail=str(e))
```

```typescript
// Frontend (React)
import { loadStripe } from '@stripe/stripe-js';
import { Elements, PaymentElement, useStripe, useElements } from '@stripe/react-stripe-js';

const stripePromise = loadStripe(process.env.NEXT_PUBLIC_STRIPE_KEY!);

function CheckoutForm() {
  const stripe = useStripe();
  const elements = useElements();

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!stripe || !elements) return;

    const { error } = await stripe.confirmPayment({
      elements,
      confirmParams: {
        return_url: `${window.location.origin}/payment-success`,
      },
    });

    if (error) {
      console.error(error.message);
    }
  };

  return (
    <form onSubmit={handleSubmit}>
      <PaymentElement />
      <button disabled={!stripe}>Pay</button>
    </form>
  );
}

function CheckoutPage({ clientSecret }: { clientSecret: string }) {
  return (
    <Elements stripe={stripePromise} options={{ clientSecret }}>
      <CheckoutForm />
    </Elements>
  );
}
```

### Subscription Billing
```python
# Create customer and subscription
def create_subscription(email: str, payment_method_id: str, price_id: str):
    # Create or retrieve customer
    customer = stripe.Customer.create(
        email=email,
        payment_method=payment_method_id,
        invoice_settings={"default_payment_method": payment_method_id},
    )

    # Create subscription
    subscription = stripe.Subscription.create(
        customer=customer.id,
        items=[{"price": price_id}],
        expand=["latest_invoice.payment_intent"],
    )

    return subscription

# Cancel subscription
def cancel_subscription(subscription_id: str, immediately: bool = False):
    if immediately:
        return stripe.Subscription.delete(subscription_id)
    else:
        # Cancel at period end
        return stripe.Subscription.modify(
            subscription_id,
            cancel_at_period_end=True
        )

# Update subscription
def update_subscription(subscription_id: str, new_price_id: str):
    subscription = stripe.Subscription.retrieve(subscription_id)
    return stripe.Subscription.modify(
        subscription_id,
        items=[{
            "id": subscription["items"]["data"][0].id,
            "price": new_price_id,
        }],
        proration_behavior="create_prorations",
    )
```

### Webhook Handling
```python
from fastapi import Request, HTTPException
import stripe

WEBHOOK_SECRET = os.environ["STRIPE_WEBHOOK_SECRET"]

@app.post("/webhooks/stripe")
async def stripe_webhook(request: Request):
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature")

    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, WEBHOOK_SECRET
        )
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid payload")
    except stripe.error.SignatureVerificationError:
        raise HTTPException(status_code=400, detail="Invalid signature")

    # Handle events
    handlers = {
        "payment_intent.succeeded": handle_payment_succeeded,
        "payment_intent.failed": handle_payment_failed,
        "customer.subscription.created": handle_subscription_created,
        "customer.subscription.updated": handle_subscription_updated,
        "customer.subscription.deleted": handle_subscription_deleted,
        "invoice.paid": handle_invoice_paid,
        "invoice.payment_failed": handle_invoice_payment_failed,
    }

    handler = handlers.get(event["type"])
    if handler:
        await handler(event["data"]["object"])

    return {"status": "success"}

async def handle_payment_succeeded(payment_intent):
    order_id = payment_intent["metadata"].get("order_id")
    if order_id:
        await update_order_status(order_id, "paid")
        await send_confirmation_email(order_id)

async def handle_subscription_deleted(subscription):
    customer_id = subscription["customer"]
    await deactivate_user_subscription(customer_id)
```

## PayPal Integration

### Setup
```python
from paypalserversdk import PaypalServerSDK

paypal = PaypalServerSDK(
    client_id=os.environ["PAYPAL_CLIENT_ID"],
    client_secret=os.environ["PAYPAL_CLIENT_SECRET"],
    environment="sandbox"  # or "production"
)
```

### Create Order
```python
def create_paypal_order(amount: str, currency: str = "USD"):
    request = {
        "intent": "CAPTURE",
        "purchase_units": [{
            "amount": {
                "currency_code": currency,
                "value": amount,
            },
            "description": "Order description",
        }],
    }
    
    response = paypal.orders.create(request)
    return response.result.id

@app.post("/api/paypal/create-order")
async def create_order(request: OrderRequest):
    order_id = create_paypal_order(str(request.amount))
    return {"orderId": order_id}
```

### Capture Payment
```python
def capture_paypal_order(order_id: str):
    response = paypal.orders.capture(order_id)
    return response.result

@app.post("/api/paypal/capture-order/{order_id}")
async def capture_order(order_id: str):
    result = capture_paypal_order(order_id)
    if result.status == "COMPLETED":
        await process_successful_payment(result)
    return {"status": result.status}
```

## PCI Compliance

### PCI-DSS Requirements Summary

| Requirement | Description                                        |
| ----------- | -------------------------------------------------- |
| 1-2         | Network security (firewalls, secure config)        |
| 3           | Protect stored cardholder data                     |
| 4           | Encrypt transmission of cardholder data            |
| 5-6         | Vulnerability management (antivirus, secure dev)   |
| 7-8-9       | Access control (restrict, identify, physical)      |
| 10-11       | Monitoring (track access, test security)           |
| 12          | Information security policy                        |

### SAQ Types

| SAQ   | Scenario                              |
| ----- | ------------------------------------- |
| SAQ-A | Fully outsourced (iframe/redirect)    |
| SAQ-A EP | Hosted payment page on your domain |
| SAQ-D | Full cardholder data handling         |

### Security Best Practices
```python
# NEVER store raw card data
# BAD - Don't do this
# card_number = request.card_number
# db.save(card_number)

# GOOD - Use tokenization
payment_method = stripe.PaymentMethod.create(
    type="card",
    card={"token": card_token},  # From Stripe.js
)

# Store only references
db.save({
    "stripe_customer_id": customer.id,
    "stripe_payment_method_id": payment_method.id,
})
```

### Secure Transmission
```python
# Always use HTTPS
# Verify webhook signatures
# Use TLS 1.2+

# Rate limiting for payment endpoints
from slowapi import Limiter
from slowapi.util import get_remote_address

limiter = Limiter(key_func=get_remote_address)

@app.post("/api/create-payment")
@limiter.limit("5/minute")
async def create_payment(request: Request):
    pass

# Log security events (without sensitive data)
logger.info(f"Payment attempt for customer {customer_id}", extra={
    "event": "payment_attempt",
    "customer_id": customer_id,
    "amount": amount,
    # Never log card numbers, CVV, etc.
})
```

## Error Handling

### Stripe Errors
```python
import stripe

def handle_payment(payment_method_id: str, amount: int):
    try:
        intent = stripe.PaymentIntent.create(
            amount=amount,
            currency="usd",
            payment_method=payment_method_id,
            confirm=True,
        )
        return {"success": True, "intent": intent}
    except stripe.error.CardError as e:
        # Card declined
        return {"success": False, "error": e.user_message, "code": e.code}
    except stripe.error.RateLimitError:
        # Too many requests
        return {"success": False, "error": "Please try again", "retry": True}
    except stripe.error.InvalidRequestError as e:
        # Invalid parameters
        return {"success": False, "error": str(e)}
    except stripe.error.AuthenticationError:
        # API key issues
        logger.error("Stripe authentication failed")
        return {"success": False, "error": "Configuration error"}
    except stripe.error.APIConnectionError:
        # Network issues
        return {"success": False, "error": "Network error", "retry": True}
    except stripe.error.StripeError as e:
        # Generic Stripe error
        logger.error(f"Stripe error: {e}")
        return {"success": False, "error": "Payment failed"}
```

### Common Error Codes
| Code                  | Meaning                | User Action                  |
| --------------------- | ---------------------- | ---------------------------- |
| card_declined         | Card was declined      | Try another card             |
| insufficient_funds    | Not enough funds       | Try another card             |
| expired_card          | Card is expired        | Update card info             |
| incorrect_cvc         | CVC is wrong           | Re-enter CVC                 |
| processing_error      | Processing failed      | Try again                    |
| incorrect_number      | Card number is wrong   | Check card number            |

## Refunds

```python
def process_refund(payment_intent_id: str, amount: int = None, reason: str = None):
    """
    Process full or partial refund.
    
    Args:
        payment_intent_id: Original payment intent ID
        amount: Amount to refund in cents (None for full refund)
        reason: One of: duplicate, fraudulent, requested_by_customer
    """
    refund_params = {
        "payment_intent": payment_intent_id,
    }
    
    if amount:
        refund_params["amount"] = amount
    if reason:
        refund_params["reason"] = reason

    return stripe.Refund.create(**refund_params)
```

## Implementation Checklist
- [ ] Use Stripe Elements or PayPal JS SDK (never raw card input)
- [ ] Webhook signature verification enabled
- [ ] Idempotency keys for retries
- [ ] Error handling for all payment errors
- [ ] Secure storage (tokens only, no raw card data)
- [ ] HTTPS everywhere
- [ ] Rate limiting on payment endpoints
- [ ] Audit logging (without sensitive data)
- [ ] Test mode for development
- [ ] Subscription lifecycle handling
- [ ] Refund flow implemented
