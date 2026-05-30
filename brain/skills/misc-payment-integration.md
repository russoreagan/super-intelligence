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
